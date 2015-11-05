"""
Neighborhood Space Data Models
******************************

"""
import calendar
from datetime import date, timedelta, datetime
import logging

from dateutil.relativedelta import relativedelta
from django.db import models
from django.db.models import Q
from contracts.models import LeaseContract, ManagementContract
from finances.models import Invoice, InvoiceType
from maintenance.models import MaintenanceRequest
from ns_helpers.helpers import Addressable
from unified_messages.models import Message

from unit_manager.helpers import angular_sref


class Property(Addressable):
    """ A Property """
    class Meta:
        verbose_name_plural = "properties"
    owners = models.ManyToManyField('user_profiles.UserProfile', blank=True)
    manager = models.ManyToManyField('user_profiles.UserProfile', through=ManagementContract,
                                     through_fields=('property', 'manager'),
                                     related_name="managed_property")
    profile = models.OneToOneField('PropertyProfile')

    def __unicode__(self):
        return "%s %s %s, %s %s" % (self.address1, self.address2, self.city,
                                    self.state, self.zip)

    def get_tenants(self, today):
        """ Get the active tenants for this property

        :param today:   Date to determine active leases
        :type today:    datetime.date
        :return:        list of active UserProfiles
        :rtype:         list of UserProfiles
        """
        return [lease.tenant for lease in self.leasecontract_set.all()
                if lease.start_date <= today and lease.end_date >= today]

    def get_user_roles(self, user_profile, today):
        """ Get the user roles for the property

        Can be 'tenant', 'manager', 'owner'.

        :param user_profile:    User Profile to query
        :type user_profile:     UserProfile
        :param today:           Date to query for.  For contract validity.
        :type today:            datetime.date
        :return:                A tuple of roles, or an empty list if none apply
        :rtype:                 tuple
        """
        ret = []

        # Is the user an owner?
        if user_profile in self.owners.all():
            ret.append('owner')

        # Is the user a manager?
        if len(self.managementcontract_set.filter(manager=user_profile,
                                                  start_date__lte=today,
                                                  end_date__gte=today)) > 0:
            ret.append('manager')

        # Is the user a tenant?
        if len(self.leasecontract_set.filter(tenant=user_profile,
                                             start_date__lte=today,
                                             end_date__gte=today)) > 0:
            ret.append('tenant')

        return tuple(ret)

    def get_active_leases(self, today):
        """ Get leases that are currently in effect for this property

        :param today:       The date to look for active leases around
        :type today:        datetime.date
        :return:            Query set of active leases
        :rtype:             django.models.QuerySet
        """
        return LeaseContract.objects.filter(property=self,
                                            start_date__lte=today,
                                            end_date__gte=today)

    def get_rent_status(self, today):
        """ Get the rent status

        Paid, due, late, NA

        NA is for properties without a tenant, so no rents would be due or
        collected.

        :param today:       The date to use for the calculation
        :type today:        datetime.date
        :return:            Rent status string
        :rtype:             str
        """
        ret = "!!"
        try:
            leases = self.get_active_leases(today=date.today())
            for lease in leases:
                if today < lease.start_date:
                    logging.debug("Lease not started")
                    return "NA"

                grace_period = lease.days_grace_period

                try:
                    this_month_due_date = date(year=today.year, month=today.month,
                                               day=lease.rent_due_day)
                except ValueError:
                    # Failover to the last day of the month if day is unavailable
                    this_month_due_date = date(year=today.year, month=today.month,
                                               day=calendar.monthrange(today.year,
                                                                       today.month)[1])
                logging.debug("This month's due date: %s" % this_month_due_date)
                grace_period = timedelta(days=grace_period)

                ret = "Late"
                if today < this_month_due_date:
                    last_due = this_month_due_date-relativedelta(months=1)
                    if last_due.day != lease.rent_due_day:
                        last_due += relativedelta(day=lease.rent_due_day)
                else:  # today >= this_month_due_date
                    last_due = this_month_due_date
                logging.debug("Rent was last due on %s. Today: %s" % (last_due, today))

                # Check if an invoice exists for the date.
                rent_type = InvoiceType.objects.get(name="Rent")
                logging.debug("Getting invoice for rent due %s" % last_due)
                invoice = Invoice.objects.get(type=rent_type, due_date=last_due,
                                              payer=lease.tenant, )
                logging.debug("Found invoice issued %s due %s" % (invoice.issued_date,
                                                                  invoice.due_date))
                if invoice.paid_date:
                    ret = "Paid"
                    logging.debug("Rent was paid on %s" % invoice.paid_date)
                else:
                    if last_due <= today <= last_due+grace_period:
                        ret = "Due"
                        logging.debug("In grace period [%s, %s]" % (last_due,
                                                                    last_due+grace_period))
        except Invoice.DoesNotExist:
            logging.debug("No invoice found")
            ret = "!!"
        except LeaseContract.DoesNotExist:
            logging.debug("No lease contract found")
            ret = "NA"

        return ret

    @staticmethod
    def build_event(headline, date, person, actionText, action, type):
        """ Create an event for the event list

        Event types:  management, lease, message, maintenance

        :param headline:    Headline for the event list
        :type headline:     str
        :param date:        Date to insert the event
        :type date:         datetime.datetime
        :param person:      Person involved
        :type person:       UserProfile
        :param actionText:  Text for the button
        :type actionText:   str
        :param action:      URL to go to on click
        :type action:       str
        :param type:        The type of event this is.
        :type type:         str
        :return:            The populated entry
        :rtype:             dict
        """
        return tuple({'headline': headline, 'date': date, 'person': person,
                'actionText': actionText, 'action': action,
                'type': type}.iteritems())

    def get_activity(self, user, today):
        """ Get activity for a property from a history of events.

        Events include:
            * Invoices
            * Contracts
            * Maintenance requests
            * Messages

        Returns a dictionary of event dictionaries.  The dictionaries are formatted
        as such:

            * headline - the headline of the activity
            * date - the date of the activity
            * person - The person involved in this activity with the current user.
                       Can sometimes be the current user, as with leases.
            * action - The link to connect to for the button on the event.  If None,
                       no button will be shown.
            * actionText - Button text.  If None, no button will be shown.

        :param user:        The user to generate events for
        :type user:         auth.models.UserProfile
        :param today:       The date to use for the day the activities are being
                            viewed
        :type today:        datetime.date
        :return:            List of event dicts
        :rtype:             list of dict
        """
        activity = []

        # Get any lease information for the user and property
        leases = LeaseContract.objects.filter(tenant=user, property=self)
        for lease in leases:
            event = self.build_event("Lease Started", lease.start_date,
                                     lease.tenant, 'View Lease',
                                     angular_sref("lease-detail", args=(lease.id,)),
                                     'lease')
            activity.append(event)
            if lease.end_date <= today:
                event = self.build_event("Lease Ended", lease.end_date,
                                         lease.tenant, 'View Lease',
                                         angular_sref("lease-detail", args=(lease.id,)),
                                         'lease')
                activity.append(event)

        if user in self.owners.all():
            leases = LeaseContract.objects.filter(property=self)
            for lease in leases:
                event = self.build_event("Lease Started", lease.start_date,
                                         lease.tenant, 'View Lease',
                                         angular_sref("lease-detail", args=(lease.id,)),
                                         'lease')
                activity.append(event)
                if lease.end_date <= today:
                    event = self.build_event("Lease Ended", lease.end_date,
                                             lease.tenant, 'View Lease',
                                             angular_sref("lease-detail", args=(lease.id,)),
                                             'lease')
                    activity.append(event)

        leases = LeaseContract.objects.filter(property__manager=user)
        for lease in leases:
            event = self.build_event("Lease Started", lease.start_date,
                                     lease.tenant, 'View Lease',
                                     angular_sref("lease-detail", args=(lease.id,)),
                                     'lease')
            activity.append(event)
            if lease.end_date <= today:
                event = self.build_event("Lease Ended", lease.end_date,
                                         lease.tenant, 'View Lease',
                                         angular_sref("lease-detail", args=(lease.id,)),
                                         'lease')
                activity.append(event)

        # Get any managing information for the user and property
        mgmt_contracts = ManagementContract.objects.filter(manager=user,
                                                           property=self)
        for contract in mgmt_contracts:
            event = self.build_event('Management Started', contract.start_date,
                                     contract.owner, 'View Contract',
                                     angular_sref("mgmt-contract-detail",
                                                  args=(contract.id,)),
                                     'management')
            activity.append(event)
            if contract.end_date <= today:
                event = self.build_event('Management Ended', contract.end_date,
                                         contract.owner, 'View Contract',
                                         angular_sref("mgmt-contract-detail",
                                                      args=(contract.id,)),
                                         'management')
                activity.append(event)

        # Get any maintenance requests involving this property
        assigned_requests = MaintenanceRequest.objects.filter(assignee=user,
                                                              property=self)
        for request in assigned_requests:
            event = self.build_event("New Request: %s" % request.headline,
                                     request.creation_date, contract.created_by,
                                     'View Request',
                                     angular_sref("mgmt-contract-detail",
                                                  args=(contract.id,)),
                                     'maintenance')
            activity.append(event)

            if request.resolution_date and request.resolution_date <= today:
                event = self.build_event("Closed Request: %s" % request.headline,
                                         request.creation_date,
                                         contract.assignee,
                                         'View Request',
                                         angular_sref("mgmt-contract-detail",
                                                      args=(contract.id,)),
                                         'maintenance')
                activity.append(event)

            if request.assigned_date and request.assigned_date <= today:
                event = self.build_event("Assigned Request: %s" % request.headline,
                                         request.assigned_date,
                                         contract.assignee,
                                         'View Request',
                                         angular_sref("mgmt-contract-detail",
                                                      args=(contract.id,)),
                                         'maintenance')
                activity.append(event)

        created_requests = MaintenanceRequest.objects.filter(created_by=user,
                                                             property=self)
        for request in created_requests:
            event = self.build_event("New Request: %s" % request.headline,
                                     request.creation_date, request.created_by,
                                     'View Request',
                                     angular_sref("maintenance-detail",
                                                  args=(request.id,)),
                                     'maintenance')
            activity.append(event)

            if request.resolution_date and request.resolution_date <= today:
                event = self.build_event("Closed Request: %s" % request.headline,
                                         request.creation_date,
                                         contract.assignee,
                                         'View Request',
                                         angular_sref("maintenance-detail",
                                                      args=(request.id,)),
                                         'maintenance')
                activity.append(event)

            if request.assigned_date and request.assigned_date <= today:
                event = self.build_event("Assigned Request: %s" % request.headline,
                                         request.assigned_date,
                                         contract.assignee,
                                         'View Request',
                                         angular_sref("maintenance-detail",
                                                      args=(request.id,)),
                                         'maintenance')
                activity.append(event)

        # Any messages between you and the tenant?  If you are the tenant, show
        # communication with others.
        tenants = self.get_tenants(date.today())
        if len(tenants) > 0:
            values = [user.user.email, user.phone1, user.phone2]
            values.extend(tenant.user.email for tenant in tenants)
            values.extend(tenant.phone1 for tenant in tenants)
            values.extend(tenant.phone2 for tenant in tenants if not tenant.phone2 is None)

            fields = Q()
            for x in values:
                fields |= Q(sender=x) | Q(recipients=x)

            messages = Message.objects.filter(fields, fields, property=self, user_profile=user)
            for message in messages:
                event = self.build_event("%s: %s" % (message.type.name, message.headline),
                                         message.creation_date, user, 'View',
                                         angular_sref("message-detail", args=(message.id,)),
                                         message.type.name)
                activity.append(event)

        #
        # Get invoices
        #
        invoices = Invoice.objects.filter(Q(payer=user) | Q(payee=user),
                                          property=self)
        for invoice in invoices:
            event = self.build_event("%s Created: %s" % (invoice.type.name, str(invoice.amount())),
                                     invoice.issued_date,
                                     invoice.payer if invoice.payer != user else invoice.payee,
                                     'View Invoice',
                                     angular_sref("invoice-detail",
                                                  args=(invoice.id,)),
                                     'invoice')
            activity.append(event)
            if invoice.due_date and invoice.due_date <= today:
                event = self.build_event("%s Due: %s" % (invoice.type.name, str(invoice.amount())),
                                         invoice.due_date,
                                         invoice.payer if invoice.payer != user else invoice.payee,
                                         'View Invoice',
                                         angular_sref("invoice-detail",
                                                      args=(invoice.id,)),
                                         'invoice')
                activity.append(event)
            if invoice.paid_date and invoice.paid_date <= today:
                event = self.build_event("%s Paid: %s" % (invoice.type.name, str(invoice.amount())),
                                         invoice.paid_date,
                                         invoice.payer if invoice.payer != user else invoice.payee,
                                         'View Invoice',
                                         angular_sref("invoice-detail",
                                                      args=(invoice.id,)),
                                         'invoice')
                activity.append(event)
        activity = [dict(x) for x in set(activity)]
        activity.sort(key=lambda event: event['date'].date() if type(event['date']) == datetime else event['date'], reverse=True)
        return activity


class PropertyProfile(models.Model):
    """ A description of a property """
    type = models.CharField(choices=(('th', "Townhome"), ('ap', "Apartment"),
                                     ('sf', "Single Family"), ('co', "Condo")),
                            max_length=2)
    description = models.TextField(null=True, blank=True)
    bedrooms = models.IntegerField()
    baths = models.DecimalField(max_digits=5, decimal_places=2)
    parking = models.CharField(choices=(('ga', "Garage"), ('st', "Street"),
                                        ('co', "Covered"), ('no', "None")),
                               max_length=2)
    sqft = models.IntegerField()
    lot_size_acres = models.DecimalField(max_digits=6, decimal_places=3)
    main_image = models.ForeignKey('PropertyImage', null=True, blank=True)

    def __unicode__(self):
        return "Profile: %s" % self.property.address1


class PropertyImage(models.Model):
    """ Images of a property """
    property_profile = models.ForeignKey(PropertyProfile)
    link = models.URLField()
    caption = models.CharField(max_length=255, blank=True)

    def __unicode__(self):
        return "Image for %s" % self.property_profile.property.address1


class PropertyListing(models.Model):
    """ For publishing an availability """
    property = models.OneToOneField(Property)
    rent = models.DecimalField(decimal_places=2, max_digits=10, default=0.)
    allow_pets = models.BooleanField(default=True)
    pet_fee_flat = models.DecimalField(decimal_places=2, max_digits=10,
                                       default=0.)
    pet_fee_pct = models.DecimalField(decimal_places=2, max_digits=10,
                                      default=0.)
    pet_rent_flat = models.DecimalField(decimal_places=2, max_digits=10,
                                        default=0.)
    pet_rent_pct = models.DecimalField(decimal_places=2, max_digits=10,
                                       default=0.)
    max_pets = models.IntegerField(default=0)
    furnished = models.BooleanField(default=False)

    headline = models.CharField(max_length=512)
    subheadline = models.CharField(max_length=512, null=True, blank=True)
    description = models.TextField()
    location_description = models.TextField(null=True, blank=True)
    amenities_description = models.TextField(null=True, blank=True)
    contact = models.ForeignKey('user_profiles.UserProfile')

    active = models.BooleanField(default=False)

    def __unicode__(self):
        return "Listing for: %s" % self.property.get_full_street_address()


class Event(models.Model):
    """ Any even that needs to be generated by the system """
    name = models.CharField(max_length=255)

    def __unicode__(self):
        return self.name


class Furnishing(models.Model):
    """ Furniture, appliances, etc on a property that we need to track """
    owner = models.ForeignKey('user_profiles.UserProfile')
    property = models.ForeignKey(Property)
    name = models.CharField(max_length=128)
    description = models.TextField(null=True, blank=True)
    image = models.URLField(null=True, blank=True)

    def __unicode__(self):
        return "%s's %s" % (self.owner, self.name)


class AccessControl(models.Model):
    """ Means of accessing a building - keys, codes, etc """
    property = models.ForeignKey(Property)
    type = models.CharField(choices=(('k', "Key"), ('c', "Code"),
                                     ('g', "Garage Opener")),
                            max_length=1)
    owner = models.ForeignKey('user_profiles.UserProfile')
    note = models.CharField(max_length=128, null=True, blank=True)
    image = models.URLField(null=True, blank=True)

    def __unicode__(self):
        return "%s %s" % (self.property.get_full_street_address(), self.get_type_display())


