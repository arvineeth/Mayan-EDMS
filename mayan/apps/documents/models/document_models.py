import logging
import uuid

from django.apps import apps
from django.core.files import File
from django.db import models, transaction
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import ugettext, ugettext_lazy as _

from mayan.apps.common.signals import signal_mayan_pre_save
from mayan.apps.events.classes import EventManagerSave
from mayan.apps.events.decorators import method_event

from ..events import (
    event_document_create, event_document_properties_edit,
    event_document_trashed, event_document_type_changed,
)
from ..literals import (
    DEFAULT_LANGUAGE, DOCUMENT_FILE_ACTION_PAGES_APPEND,
    DOCUMENT_FILE_ACTION_PAGES_KEEP, DOCUMENT_FILE_ACTION_PAGES_NEW
)
from ..managers import DocumentManager, TrashCanManager, ValidDocumentManager
from ..signals import signal_post_document_type_change

from .document_type_models import DocumentType

__all__ = ('Document',)
logger = logging.getLogger(name=__name__)


class HooksMixin:
    @classmethod
    def _execute_hooks(cls, hook_list, **kwargs):
        result = None

        for hook in hook_list:
            result = hook(**kwargs)
            if result:
                kwargs.update(result)

        return result

    @classmethod
    def _insert_hook_entry(cls, hook_list, func, order=None):
        order = order or len(hook_list)
        hook_list.insert(order, func)


class Document(HooksMixin, models.Model):
    """
    Defines a single document with it's fields and properties
    Fields:
    * uuid - UUID of a document, universally Unique ID. An unique identifier
    generated for each document. No two documents can ever have the same UUID.
    This ID is generated automatically.
    """
    _hooks_pre_create = []

    uuid = models.UUIDField(
        default=uuid.uuid4, editable=False, help_text=_(
            'UUID of a document, universally Unique ID. An unique identifier '
            'generated for each document.'
        ), verbose_name=_('UUID')
    )
    document_type = models.ForeignKey(
        help_text=_('The document type of the document.'),
        on_delete=models.CASCADE, related_name='documents', to=DocumentType,
        verbose_name=_('Document type')
    )
    label = models.CharField(
        blank=True, db_index=True, default='', max_length=255,
        help_text=_('The name of the document.'), verbose_name=_('Label')
    )
    description = models.TextField(
        blank=True, default='', help_text=_(
            'An optional short text describing a document.'
        ), verbose_name=_('Description')
    )
    date_added = models.DateTimeField(
        auto_now_add=True, db_index=True, help_text=_(
            'The server date and time when the document was finally '
            'processed and added to the system.'
        ), verbose_name=_('Added')
    )
    language = models.CharField(
        blank=True, default=DEFAULT_LANGUAGE, help_text=_(
            'The dominant language in the document.'
        ), max_length=8, verbose_name=_('Language')
    )
    in_trash = models.BooleanField(
        db_index=True, default=False, help_text=_(
            'Whether or not this document is in the trash.'
        ), editable=False, verbose_name=_('In trash?')
    )
    deleted_date_time = models.DateTimeField(
        blank=True, editable=True, help_text=_(
            'The server date and time when the document was moved to the '
            'trash.'
        ), null=True, verbose_name=_('Date and time trashed')
    )
    is_stub = models.BooleanField(
        db_index=True, default=True, editable=False, help_text=_(
            'A document stub is a document with an entry on the database but '
            'no file uploaded. This could be an interrupted upload or a '
            'deferred upload via the API.'
        ), verbose_name=_('Is stub?')
    )

    objects = DocumentManager()
    trash = TrashCanManager()
    valid = ValidDocumentManager()

    @classmethod
    def execute_pre_create_hooks(cls, kwargs=None):
        """
        Helper method to allow checking if it is possible to create
        a new document.
        """
        cls._execute_hooks(
            hook_list=cls._hooks_pre_create, kwargs=kwargs
        )

    @classmethod
    def register_pre_create_hook(cls, func, order=None):
        cls._insert_hook_entry(
            hook_list=cls._hooks_pre_create, func=func, order=order
        )

    class Meta:
        ordering = ('label',)
        verbose_name = _('Document')
        verbose_name_plural = _('Documents')

    def __str__(self):
        return self.label or ugettext('Document stub, id: %d') % self.pk

    def add_as_recent_document_for_user(self, user):
        RecentDocument = apps.get_model(
            app_label='documents', model_name='RecentDocument'
        )
        return RecentDocument.objects.add_document_for_user(
            document=self, user=user
        )

    #@property
    #def checksum(self):
    #    return self.latest_file.checksum

    #@property
    #def date_updated(self):
    #    return self.latest_file.timestamp

    def delete(self, *args, **kwargs):
        to_trash = kwargs.pop('to_trash', True)
        _user = kwargs.pop('_user', None)

        if not self.in_trash and to_trash:
            self.in_trash = True
            self.deleted_date_time = now()
            with transaction.atomic():
                #self.save(_commit_events=False)
                self._event_ignore = True
                self.save()
                event_document_trashed.commit(actor=_user, target=self)
        else:
            with transaction.atomic():
                for document_file in self.files.all():
                    document_file.delete()

                return super().delete(*args, **kwargs)

    def document_type_change(self, document_type, force=False, _user=None):
        has_changed = self.document_type != document_type

        self.document_type = document_type
        with transaction.atomic():
            self.save()
            if has_changed or force:
                signal_post_document_type_change.send(
                    sender=self.__class__, instance=self
                )

                event_document_type_changed.commit(actor=_user, target=self)
                if _user:
                    self.add_as_recent_document_for_user(user=_user)

    #def exists(self):
    #    """
    #    Returns a boolean value that indicates if the document's
    #    latest file file exists in storage
    #    """
    #    latest_file = self.latest_file
    #    if latest_file:
    #        return latest_file.exists()
    #    else:
    #        return False

    #@property
    #def file_mime_encoding(self):
    #    return self.latest_file.encoding

    #@property
    #def file_mimetype(self):
    #    return self.latest_file.mimetype

    def get_absolute_url(self):
        return reverse(
            viewname='documents:document_preview', kwargs={
                'document_id': self.pk
            }
        )

    def get_api_image_url(self, *args, **kwargs):
        latest_version = self.latest_version
        if latest_version:
            return latest_version.get_api_image_url(*args, **kwargs)

    @property
    def is_in_trash(self):
        return self.in_trash

    @property
    def latest_file(self):
        return self.files.order_by('timestamp').last()

    @property
    def latest_version(self):
        return self.versions.order_by('timestamp').last()

    def natural_key(self):
        return (self.uuid,)
    natural_key.dependencies = ['documents.DocumentType']

    def new_file(self, file_object, action=None, comment=None, _user=None):
        logger.info('Creating new document file for document: %s', self)

        if not action:
            action = DOCUMENT_FILE_ACTION_PAGES_NEW

        if not comment:
            comment = ''

        DocumentFile = apps.get_model(
            app_label='documents', model_name='DocumentFile'
        )
        #transaction.atomic
        try:
            document_file = DocumentFile(
                document=self, comment=comment, file=File(file=file_object)
            )
            #document_file = self.files(
            #    comment=comment or '', file=File(file=file_object)
            #)
            document_file.save(_user=_user)
        except Exception as exception:
            logger.error('Error creating new file for document: %s', self)
            raise
        else:
            logger.info('New document file queued for document: %s', self)

            if action == DOCUMENT_FILE_ACTION_PAGES_NEW:
                document_version = self.versions.create(comment=comment)
                document_version.pages_remap(
                    content_object_list=list(document_file.pages.all())
                )
            elif action == DOCUMENT_FILE_ACTION_PAGES_APPEND:
                content_object_list = []
                content_object_list.extend(
                    self.latest_version.page_content_objects
                )
                content_object_list.extend(list(document_file.pages.all()))

                document_version = self.versions.create(comment=comment)
                document_version.pages_remap(
                    content_object_list=content_object_list
                )
            elif action == DOCUMENT_FILE_ACTION_PAGES_KEEP:
                return document_file

            return document_file

    #def open(self, *args, **kwargs):
    #    """
    #    Return a file descriptor to a document's file irrespective of
    #    the storage backend
    #    """
    #    return self.latest_file.open(*args, **kwargs)

    @property
    def page_count(self):
        return self.pages.count()

    @property
    def pages(self):
        try:
            return self.latest_version.pages
        except AttributeError:
            # Document has no version yet
            DocumentVersionPage = apps.get_model(
                app_label='documents', model_name='DocumentVersionPage'
            )

            return DocumentVersionPage.objects.none()

    def restore(self):
        self.in_trash = False
        self.save()

    @method_event(
        event_manager_class=EventManagerSave,
        created={
            'event': event_document_create,
            'action_object': 'document_type',
            'keep_attributes': '_event_actor',
            'target': 'self'
        },
        edited={
            'event': event_document_properties_edit,
            'action_object': 'document_type',
            'target': 'self'
        }
    )
    def save(self, *args, **kwargs):
        user = kwargs.pop('_event_actor', None)
        #_commit_events = kwargs.pop('_commit_events', True)
        new_document = not self.pk

        signal_mayan_pre_save.send(
            sender=Document, instance=self, user=user
        )

        super().save(*args, **kwargs)

        if new_document:
            if user:
                self.add_as_recent_document_for_user(user=user)

                #event_document_create.commit(
                #    actor=user, action_object=self.document_type,
                #    target=self
                #)
            #else:
            #    if _commit_events:
            #        event_document_properties_edit.commit(actor=user, target=self)

    #def save_to_file(self, *args, **kwargs):
    #    return self.latest_file.save_to_file(*args, **kwargs)

    #@property
    #def size(self):
    #    return self.latest_file.size


    #test method for development
    #def versions_create(self):
    #    with transaction.atomic():
    #        document_version = self.versions.create()
    #        document_version.pages_reset()


class TrashedDocument(Document):
    objects = TrashCanManager()

    class Meta:
        proxy = True
