import base64
import email
import datetime
import logging
import os
import quopri
from dateutil.parser import parse
from django.db.models import Q
from django.shortcuts import redirect
from django.views.generic import RedirectView, ListView
import httplib2
from oauth2client import xsrfutil
from oauth2client.client import flow_from_clientsecrets, Storage
import twython
from neighborhood_space import settings
from ns_helpers.helpers import LoginRequiredMixin
from unified_messages.models import Message, GMailCredential, TwitterAuth
from unit_manager.helpers import angular_sref
from user_profiles.models import EmailAccount
from apiclient.discovery import build


CLIENT_SECRETS = os.path.join(os.path.dirname(__file__), '..',
                              'client_secret.json')


FLOW = flow_from_clientsecrets(
    CLIENT_SECRETS,
    scope='https://www.googleapis.com/auth/gmail.readonly',
    redirect_uri='http://localhost:8000/oauth2callback')


class GMailOAuthReturnView(LoginRequiredMixin, RedirectView):
    """ Receive the return from GMail authentication """
    pattern_name = "message-email"

    def get_redirect_url(self, *args, **kwargs):
        #if not xsrfutil.validate_token(settings.SECRET_KEY, self.request.REQUEST['state'], self.request.user):
        #    raise http.Http404()

        credential = FLOW.step2_exchange(self.request.REQUEST)
        storage = Storage(GMailCredential, 'id', self.request.user, 'credential')
        storage.put(credential)

        return super(GMailOAuthReturnView, self).get_redirect_url(*args,
                                                                  **kwargs)


class MessageEmailView(LoginRequiredMixin, ListView):
    """ Email Center View """
    def get_message(self, service, user_id, msg_id):
        """ Get a gmail message

        :param service:
        :param user_id:
        :param msg_id:
        :return:
        """
        message = service.users().messages().get(userId=user_id, id=msg_id,
                                                 format='raw').execute()
        return email.message_from_string(base64.urlsafe_b64decode(message['raw'].encode('ASCII')))

    def dispatch(self, *args, **kwargs):
        try:
            gmail_address = self.request.user.userprofile.emailaccount_set.get(type__name="GMail")
            storage = Storage(GMailCredential, 'id', self.request.user, 'credential')
            credential = storage.get()
            if credential is None or credential.invalid is True:
                FLOW.params['state'] = xsrfutil.generate_token(settings.SECRET_KEY,
                                                               self.request.user)
                authorize_url = FLOW.step1_get_authorize_url()
                return redirect(authorize_url)
            else:
                # Temporary:  Until the background service that's part of Issue #48
                # gets made, do on-demand pulls from here
                http = httplib2.Http()
                http = credential.authorize(http)
                service = build("gmail", "v1", http=http)

                response = service.users().messages().list(userId=gmail_address.address,
                                                           maxResults=10).execute()

                messages = []
                if 'messages' in response:
                    messages.extend(response['messages'])

                for message in messages:
                    if message['id']:
                        # Skip it if it's already present
                        messages = Message.objects.filter(user_profile=self.request.user.userprofile,
                                                          external_id=message['id'])
                        if len(messages) > 0:
                            continue
                    msg = self.get_message(user_id='me', msg_id=message['id'],
                                           service=service)

                    recipients = msg['to']
                    if not msg['cc'] is None:
                        recipients += " " + msg['cc']
                    sender = msg['from']
                    subject = quopri.decodestring(msg['subject'])
                    try:
                        date = parse(msg['Date'])
                    except Exception:
                        date = datetime.now()

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True)
                    else:
                        body = msg.get_payload()

                    if not date or not sender or not recipients or not subject:
                        logging.debug("Invalid email.  User: %s, ID: %s" % (self.request.user,
                                                                            message['id']))
                    Message.objects.create_email(
                        user_profile=self.request.user.userprofile,
                        externalId=message['id'],
                        created_date=date,
                        sender=sender,
                        recipients=recipients,
                        subject=subject,
                        body=body
                    )
        except EmailAccount.DoesNotExist:
            pass

        return super(MessageEmailView, self).dispatch(*args, **kwargs)

    model = Message
    template_name = "messages/email_center.html"

    def get_context_data(self, **kwargs):
        context = super(MessageEmailView, self).get_context_data(**kwargs)

        context['properties'] = self.request.user.userprofile.get_associated_properties(today=datetime.now())

        return context

    def get_queryset(self):

        qs = super(MessageEmailView, self).get_queryset()

        qs = qs.filter(user_profile=self.request.user.userprofile,
                       type__name="Email")

        return qs


from twython import Twython


class MessageSocialView(LoginRequiredMixin, ListView):
    model = Message
    template_name = "messages/social_center.html"

    def dispatch(self, *args, **kwargs):
        return super(MessageSocialView, self).dispatch(*args, **kwargs)

    def get_queryset(self):

        # Temporary until a background service is created to regularly pull
        # tweets.  Part of Issue #51
        try:
            auth = TwitterAuth.objects.get(user=self.request.user.userprofile,
                                           final=True)
            twitter = Twython(settings.TWITTER_APP_KEY,
                              settings.TWITTER_APP_SECRET,
                              auth.oauth_token,
                              auth.oauth_token_secret)

            for tweet in twitter.get_home_timeline():
                # Skip it if it's already present
                messages = Message.objects.filter(user_profile=self.request.user.userprofile,
                                                  external_id=tweet['id'])
                if len(messages) > 0:
                    continue

                Message.objects.create_tweet(
                    user_profile=self.request.user.userprofile,
                    created_date=parse(tweet['created_at']),
                    sender=tweet['user']['name'],
                    body=tweet['text'],
                    externalId=tweet['id']
                )
        except twython.TwythonRateLimitError:
            pass
        except TwitterAuth.DoesNotExist:
            pass

        qs = super(MessageSocialView, self).get_queryset()

        return qs.filter(Q(type__name="Facebook") |
                         Q(type__name="Tweet") |
                         Q(type__name="Instagram") |
                         Q(type__name="Pinterest"),
                         user_profile=self.request.user.userprofile
        )

    def get_context_data(self, **kwargs):
        context = super(MessageSocialView, self).get_context_data(**kwargs)

        try:
            context['vacant_properties'] = self.request.user.userprofile.get_vacant_properties(today=datetime.now())
            TwitterAuth.objects.get(user=self.request.user.userprofile,
                                    final=True)

        except TwitterAuth.DoesNotExist:
            # In case the process was interrupted the first time, clear the entry
            try:
                auth = TwitterAuth.objects.get(user=self.request.user.userprofile)
                auth.delete()
            except TwitterAuth.DoesNotExist:
                pass

            consumer_key = settings.TWITTER_APP_KEY
            consumer_secret = settings.TWITTER_APP_SECRET

            twitter = Twython(consumer_key, consumer_secret)
            auth = twitter.get_authentication_tokens(callback_url="http://dev.neighborhood.space:8000/twitter_callback")

            oauth_token = auth['oauth_token']
            oauth_secret = auth['oauth_token_secret']

            TwitterAuth.objects.create(user=self.request.user.userprofile,
                                       oauth_token=oauth_token,
                                       oauth_token_secret=oauth_secret)

            context['twitter_auth_url'] = auth['auth_url']

        return context


class TwitterOAuthCallbackView(LoginRequiredMixin, RedirectView):
    """ Handle redirect from Twitter Auth """

    def get_redirect_url(self, *args, **kwargs):
        try:
            twitter_auth = TwitterAuth.objects.get(user=self.request.user.userprofile,
                                                   final=False)

            twitter = Twython(settings.TWITTER_APP_KEY,
                              settings.TWITTER_APP_SECRET,
                              twitter_auth.oauth_token,
                              twitter_auth.oauth_token_secret)

            oauth_verifier = self.request.GET['oauth_verifier']
            final_step = twitter.get_authorized_tokens(oauth_verifier)
            twitter_auth.oauth_token = final_step['oauth_token']
            twitter_auth.oauth_token_secret = final_step['oauth_token_secret']
            twitter_auth.final = True
            twitter_auth.save()

        except TwitterAuth.DoesNotExist:
            # It doesn't exist, just allow the redirect
            pass

        return angular_sref("message-social")


