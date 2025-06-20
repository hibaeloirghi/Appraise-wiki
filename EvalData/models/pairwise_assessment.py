"""
Appraise evaluation framework

See LICENSE for usage details
"""
# pylint: disable=C0103,C0330,no-member
import sys
from collections import defaultdict
from json import loads
from traceback import format_exc
from zipfile import is_zipfile
from zipfile import ZipFile

from datetime import timezone

utc = timezone.utc
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import format_lazy as f
from django.utils.translation import gettext_lazy as _

from Appraise.utils import _get_logger, _compute_user_total_annotation_time
from Dashboard.models import LANGUAGE_CODES_AND_NAMES
from EvalData.models.base_models import *

# TODO: Unclear if these are needed?
# from Appraise.settings import STATIC_URL, BASE_CONTEXT

LOGGER = _get_logger(name=__name__)


@AnnotationTaskRegistry.register
class PairwiseAssessmentTask(BaseMetadata):
    """
    Models a direct assessment evaluation task.
    """

    campaign = models.ForeignKey(
        'Campaign.Campaign',
        db_index=True,
        on_delete=models.PROTECT,
        related_name='%(app_label)s_%(class)s_campaign',
        related_query_name="%(app_label)s_%(class)ss",
        verbose_name=_('Campaign'),
    )

    items = models.ManyToManyField(
        TextSegmentWithTwoTargets,
        related_name='%(app_label)s_%(class)s_items',
        related_query_name="%(app_label)s_%(class)ss",
        verbose_name=_('Items'),
    )

    requiredAnnotations = models.PositiveSmallIntegerField(
        verbose_name=_('Required annotations'),
        help_text=_(
            f(
                '(value in range=[1,{value}])',
                value=MAX_REQUIREDANNOTATIONS_VALUE,
            )
        ),
    )

    assignedTo = models.ManyToManyField(
        User,
        blank=True,
        db_index=True,
        related_name='%(app_label)s_%(class)s_assignedTo',
        related_query_name="%(app_label)s_%(class)ss",
        verbose_name=_('Assigned to'),
        help_text=_('(users working on this task)'),
    )

    batchNo = models.PositiveIntegerField(
        verbose_name=_('Batch number'), help_text=_('(1-based)')
    )

    batchData = models.ForeignKey(
        'Campaign.CampaignData',
        on_delete=models.PROTECT,
        blank=True,
        db_index=True,
        null=True,
        related_name='%(app_label)s_%(class)s_batchData',
        related_query_name="%(app_label)s_%(class)ss",
        verbose_name=_('Batch data'),
    )

    def dataName(self):
        return str(self.batchData)

    def marketName(self):
        return str(self.items.first().metadata.market)

    def marketSourceLanguage(self):
        tokens = str(self.items.first().metadata.market).split('_')
        if len(tokens) == 3 and tokens[0] in LANGUAGE_CODES_AND_NAMES.keys():
            return LANGUAGE_CODES_AND_NAMES[tokens[0]]
        return None

    def marketSourceLanguageCode(self):
        tokens = str(self.items.first().metadata.market).split('_')
        if len(tokens) == 3 and tokens[0] in LANGUAGE_CODES_AND_NAMES.keys():
            return tokens[0]
        return None

    def marketTargetLanguage(self):
        tokens = str(self.items.first().metadata.market).split('_')
        if len(tokens) == 3 and tokens[1] in LANGUAGE_CODES_AND_NAMES.keys():
            return LANGUAGE_CODES_AND_NAMES[tokens[1]]
        return None

    def marketTargetLanguageCode(self):
        tokens = str(self.items.first().metadata.market).split('_')
        if len(tokens) == 3 and tokens[1] in LANGUAGE_CODES_AND_NAMES.keys():
            return tokens[1]
        return None

    def completed_items_for_user(self, user):
        results = PairwiseAssessmentResult.objects.filter(
            task=self, activated=False, completed=True, createdBy=user
        ).values_list('item_id', flat=True)

        return len(set(results))

    def is_trusted_user(self, user):
        from Campaign.models import TrustedUser

        trusted_user = TrustedUser.objects.filter(user=user, campaign=self.campaign)
        return trusted_user.exists()

    def next_item_for_user(self, user, return_completed_items=False):
        trusted_user = self.is_trusted_user(user)

        next_item = None
        completed_items = 0
        for item in self.items.all().order_by('id'):
            result = PairwiseAssessmentResult.objects.filter(
                item=item, activated=False, completed=True, createdBy=user
            )

            if not result.exists():
                print(
                    'identified next item: {0}/{1} for trusted={2}'.format(
                        item.id, item.itemType, trusted_user
                    )
                )
                if not trusted_user or item.itemType.startswith('TGT'):
                    next_item = item
                    break

            completed_items += 1

        if not next_item:
            LOGGER.info('No next item found for task {0}'.format(self.id))
            annotations = PairwiseAssessmentResult.objects.filter(
                task=self, activated=False, completed=True
            ).values_list('item_id', flat=True)
            uniqueAnnotations = len(set(annotations))

            required_user_results = 100
            if trusted_user:
                required_user_results = 70

            _total_required = self.requiredAnnotations * required_user_results
            LOGGER.info(
                'Unique annotations={0}/{1}'.format(uniqueAnnotations, _total_required)
            )
            if uniqueAnnotations >= _total_required:
                LOGGER.info('Completing task {0}'.format(self.id))
                self.complete()
                self.save()

                # Not sure why I would complete the batch here?
                # self.batchData.complete()
                # self.batchData.save()

        if return_completed_items:
            return (next_item, completed_items)

        return next_item

    @classmethod
    def get_task_for_user(cls, user):
        for active_task in cls.objects.filter(
            assignedTo=user, activated=True, completed=False
        ).order_by('-id'):
            next_item = active_task.next_item_for_user(user)
            if next_item is not None:
                return active_task

        return None

    @classmethod
    def get_next_free_task_for_language(cls, code, campaign=None, user=None):
        print('  Looking for next free task for language: {0}'.format(code))
        print('  Campaign: {0}'.format(campaign))
        print('  User: {0}'.format(user))

        active_tasks = cls.objects.filter(
            activated=True,
            completed=False,
            items__metadata__market__targetLanguageCode=code,
        )

        print('    Number of active tasks: ({0})'.format(len(active_tasks)))

        if campaign:
            active_tasks = active_tasks.filter(campaign=campaign)

        for active_task in active_tasks.order_by('id'):
            active_users = active_task.assignedTo.count()
            if active_users < active_task.requiredAnnotations:
                if user and not user in active_task.assignedTo.all():
                    return active_task

        print('    No next free task available')
        return None

        # It seems that assignedTo is converted to an integer count.
        active_tasks = active_tasks.order_by('id').values_list(
            'id', 'requiredAnnotations', 'assignedTo'
        )

        for active_task in active_tasks:
            print(active_task)
            active_users = active_task[2] or 0
            if active_users < active_task[1]:
                return cls.objects.get(pk=active_task[0])

        return None

        # TODO: this needs to be removed.
        for active_task in active_tasks:
            market = active_task.items.first().metadata.market
            if not market.targetLanguageCode == code:
                continue

            active_users = active_task.assignedTo.count()
            if active_users < active_task.requiredAnnotations:
                return active_task

        return None

    @classmethod
    def get_next_free_task_for_language_and_campaign(cls, code, campaign):
        return cls.get_next_free_task_for_language(code, campaign)

    @classmethod
    def import_from_json(cls, campaign, batch_user, batch_data, max_count):
        """
        Creates new PairwiseAssessmentTask instances based on JSON input.
        """
        batch_meta = batch_data.metadata
        batch_name = batch_data.dataFile.name
        batch_file = batch_data.dataFile
        batch_json = None

        if batch_name.endswith('.zip'):
            if not is_zipfile(batch_file):
                _msg = 'Batch {0} not a valid ZIP archive'.format(batch_name)
                LOGGER.warn(_msg)
                return

            batch_zip = ZipFile(batch_file)
            batch_json_files = [x for x in batch_zip.namelist() if x.endswith('.json')]
            # TODO: implement proper support for multiple json files in archive.
            for batch_json_file in batch_json_files:
                batch_content = batch_zip.read(batch_json_file).decode('utf-8')
                batch_json = loads(batch_content)

        else:
            batch_json = loads(str(batch_file.read(), encoding='utf-8'))

        from datetime import datetime

        t1 = datetime.now()

        current_count = 0
        max_length_id = 0
        max_length_text = 0
        for batch_task in batch_json:
            if max_count > 0 and current_count >= max_count:
                _msg = 'Stopping after max_count={0} iterations'.format(max_count)
                LOGGER.info(_msg)

                t2 = datetime.now()
                print(t2 - t1)
                return

            print('Loading batch:', batch_name, batch_task['task']['batchNo'])

            new_items = []
            count_items = 0
            for item in batch_task['items']:
                count_items += 1

                # TODO: check if target1 + target2 should be used here
                current_length_id = len(item['sourceID'])
                current_length_text = len(item['sourceText'])

                if current_length_id > max_length_id:
                    print(current_length_id, item['sourceID'])
                    max_length_id = current_length_id

                if current_length_text > max_length_text:
                    print(
                        current_length_text,
                        item['sourceText'].encode('utf-8'),
                    )
                    max_length_text = current_length_text

                item_targets = item['targets']

                # TODO: check if 'targets' is empty or has more elements than 2
                item_tgt1_idx = item_targets[0]['targetID']
                item_tgt1_txt = item_targets[0]['targetText']

                item_tgt2_idx = None
                item_tgt2_txt = None
                if len(item_targets) > 1:
                    item_tgt2_idx = item_targets[1]['targetID']
                    item_tgt2_txt = item_targets[1]['targetText']

                context_left = item.get('contextLeft', None)
                context_right = item.get('contextRight', None)

                new_item = TextSegmentWithTwoTargets(
                    segmentID=item['sourceID'],
                    segmentText=item['sourceText'],
                    target1ID=item_tgt1_idx,
                    target1Text=item_tgt1_txt,
                    target2ID=item_tgt2_idx,
                    target2Text=item_tgt2_txt,
                    createdBy=batch_user,
                    itemID=item['itemID'],
                    itemType=item['itemType'],
                    contextLeft=context_left,
                    contextRight=context_right,
                )
                new_items.append(new_item)
            
            LOGGER.info(f'The task has {len(new_items)} items')
            current_count += 1

            # Process items in smaller batches to avoid SQLite "too many variables" error
            batch_size = 100  # SQLite limit is around 999 variables, so we use 100 to be safe
            for i in range(0, len(new_items), batch_size):
                batch_items = new_items[i:i + batch_size]
                batch_meta.textsegment_set.add(*batch_items, bulk=False)
            batch_meta.save()

            new_task = PairwiseAssessmentTask(
                campaign=campaign,
                requiredAnnotations=batch_task['task']['requiredAnnotations'],
                batchNo=batch_task['task']['batchNo'],
                batchData=batch_data,
                createdBy=batch_user,
            )
            new_task.save()

            # Process items in smaller batches to avoid SQLite "too many variables" error
            for i in range(0, len(new_items), batch_size):
                batch_items = new_items[i:i + batch_size]
                new_task.items.add(*batch_items)
            new_task.save()

            _msg = 'Success processing batch {0}, task {1}'.format(
                str(batch_data), batch_task['task']['batchNo']
            )
            LOGGER.info(_msg)

        _msg = 'Max length ID={0}, text={1}'.format(max_length_id, max_length_text)
        LOGGER.info(_msg)

        t2 = datetime.now()
        print(t2 - t1)

    # pylint: disable=E1101
    def is_valid(self):
        """
        Validates the current DA task, checking campaign and items exist.
        """
        if not hasattr(self, 'campaign') or not self.campaign.is_valid():
            return False

        if not hasattr(self, 'items'):
            return False

        for item in self.items:
            if not item.is_valid():
                return False

        return True

    def _generate_str_name(self):
        return '{0}.{1}[{2}]'.format(self.__class__.__name__, self.campaign, self.id)


class PairwiseAssessmentResult(BasePairwiseAssessmentResult):
    """
    Models a contrastive direct assessment evaluation result.
    """

    score1 = models.PositiveSmallIntegerField(
        verbose_name=_('Score (1)'),
        help_text=_('(value in range=[1,100])'),
    )

    score2 = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        verbose_name=_('Score (2)'),
        help_text=_('(value in range=[1,100])'),
    )

    start_time = models.FloatField(
        verbose_name=_('Start time'), help_text=_('(in seconds)')
    )

    end_time = models.FloatField(
        verbose_name=_('End time'), help_text=_('(in seconds)')
    )

    """

    # added a new field for freetextannotation
    freetextannotation = models.TextField(
        blank=True,  # allow empty values in forms
        null=True,   # allow NULL values in database
        verbose_name=_('free text annotation'), help_text=_('(free text annotation)')
    )
    """
    
    selected_translation = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_('Selected translation'),
        help_text=_('The translation choice selected by the user (Option1 or Option2)'),
    )

    # New fields for the three questions
    selected_advantages = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_('Selected advantages'),
        help_text=_('Comma-separated list of advantages of the selected translation')
    )

    selected_advantages_other = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Other selected advantages'),
        help_text=_('Free text for "Other" option in selected advantages')
    )

    non_selected_problems = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_('Non-selected problems'),
        help_text=_('Comma-separated list of problems with the non-selected translation')
    )

    non_selected_problems_other = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Other non-selected problems'),
        help_text=_('Free text for "Other" option in non-selected problems')
    )

    wiki_adequacy = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_('Wikipedia adequacy'),
        help_text=_('Comma-separated list of translations adequate for Wikipedia')
    )

    other_text = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Other text'),
        help_text=_('Free text for "Other" option')
    )

    # Fields for Wikipedia contribution questions
    wikipedia_familiarity = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_('Wikipedia familiarity'),
        help_text=_('How familiar are you with Wikipedia?')
    )

    other_wikipedia_familiarity_text = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Other Wikipedia familiarity'),
        help_text=_('Other - How familiar are you with Wikipedia?')
    )

    fluency_in_target_language = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name=_('Fluency or proficiency in target language'),
        help_text=_('How would you rate your fluency in the target language?')
    )


    span_diff_texts = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Span difference texts'),
        help_text=_('Text spans shown to user during annotation.')
    )


    span_diff_votes = models.TextField(
    blank=True,
    null=True,
    verbose_name=_('Votes for highlighted differences'),
    help_text=_('Stores which candidate was preferred for each highlighted difference.')
    )

    span_diff_explanations = models.TextField(blank=True, null=True)

    span_diff_other_texts = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Other text explanations for each diff'),
        help_text=_('User free-text explanations when "Other" is selected.')
    )


    # for feedback form
    feedback_options = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_('Feedback options'),
        help_text=_('What is something we can do better next time?')
    )

    other_feedback_options_text = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Other feedback options'),
        help_text=_('Other - What is something we can do better next time?')
    )

    overallExperience = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name=_('Overall experience'),
        help_text=_('How would you rate your overall experience?')
    )


    item = models.ForeignKey(
        TextSegmentWithTwoTargets,
        db_index=True,
        on_delete=models.PROTECT,
        related_name='%(app_label)s_%(class)s_item',
        related_query_name="%(app_label)s_%(class)ss",
        verbose_name=_('Item'),
    )

    task = models.ForeignKey(
        PairwiseAssessmentTask,
        blank=True,
        db_index=True,
        null=True,
        on_delete=models.PROTECT,
        related_name='%(app_label)s_%(class)s_task',
        related_query_name="%(app_label)s_%(class)ss",
        verbose_name=_('Task'),
    )

    # pylint: disable=E1136
    def _generate_str_name(self):
        return '{0}.{1}={2}+{3}'.format(
            self.__class__.__name__,
            self.item,
            self.score1,
            self.score2,
        )

    def duration(self):
        d = self.end_time - self.start_time
        return round(d, 1)

    def item_type(self):
        return self.item.itemType

    @classmethod
    def get_completed_for_user(cls, user, unique_only=True):
        _query = cls.objects.filter(createdBy=user, activated=False, completed=True)
        if unique_only:
            return _query.values_list('item__id').distinct().count()
        return _query.count()

    @classmethod
    def get_hit_status_for_user(cls, user):
        user_data = defaultdict(int)

        for user_item in cls.objects.filter(
            createdBy=user, activated=False, completed=True
        ).values_list('task__id', 'item__itemType'):
            if user_item[1].lower() != 'tgt':
                continue

            user_data[user_item[0]] += 1

        total_hits = len(user_data.keys())
        completed_hits = len([x for x in user_data.values() if x >= 70])

        return (completed_hits, total_hits)

    @classmethod
    def get_time_for_user(cls, user):
        results = cls.objects.filter(createdBy=user, activated=False, completed=True)

        timestamps = []
        for result in results:
            timestamps.append((result.start_time, result.end_time))

        return seconds_to_timedelta(_compute_user_total_annotation_time(timestamps))

    @classmethod
    def get_system_annotations(cls):
        system_scores = defaultdict(list)

        value_types = ('TGT', 'CHK')
        qs = cls.objects.filter(completed=True, item__itemType__in=value_types)

        value_names = (
            'item__target1ID',
            'score1',
            'target2ID',
            'score2',
            'createdBy',
            'item__itemID',
            'item__metadata__market__sourceLanguageCode',
            'item__metadata__market__targetLanguageCode',
        )
        for result in qs.values_list(*value_names):
            systemID = result[0]
            score1 = result[1]
            score2 = result[2]
            annotatorID = result[3]
            segmentID = result[4]
            marketID = '{0}-{1}'.format(result[5], result[6])
            system_scores[marketID].append(
                (systemID, annotatorID, segmentID, score1, score2)
            )

        return system_scores

    @classmethod
    def compute_accurate_group_status(cls):
        from Dashboard.models import LANGUAGE_CODES_AND_NAMES

        user_status = defaultdict(list)
        qs = cls.objects.filter(completed=True)

        value_names = ('createdBy', 'item__itemType', 'task__id')
        for result in qs.values_list(*value_names):
            if result[1].lower() != 'tgt':
                continue

            annotatorID = result[0]
            taskID = result[2]
            user_status[annotatorID].append(taskID)

        group_status = defaultdict(list)
        for annotatorID in user_status:
            user = User.objects.get(pk=annotatorID)
            usergroups = ';'.join(
                [
                    x.name
                    for x in user.groups.all()
                    if not x.name in LANGUAGE_CODES_AND_NAMES.keys()
                ]
            )
            if not usergroups:
                usergroups = 'NoGroupInfo'

            group_status[usergroups].extend(user_status[annotatorID])

        group_hits = {}
        for group_name in group_status:
            task_ids = set(group_status[group_name])
            completed_tasks = 0
            for task_id in task_ids:
                if group_status[group_name].count(task_id) >= 70:
                    completed_tasks += 1

            group_hits[group_name] = (completed_tasks, len(task_ids))

        return group_hits

    @classmethod
    def dump_all_results_to_csv_file(cls, csv_file):
        from Dashboard.models import LANGUAGE_CODES_AND_NAMES

        system_scores = defaultdict(list)
        user_data = {}
        qs = cls.objects.filter(completed=True)

        value_names = (
            'item__target1ID',
            'score1',
            'item__target2ID',
            'score2',
            'start_time',
            'end_time',
            'createdBy',
            'item__itemID',
            'item__metadata__market__sourceLanguageCode',
            'item__metadata__market__targetLanguageCode',
            'item__metadata__market__domainName',
            'item__itemType',
            'task__id',
            'task__campaign__campaignName',
        )
        for result in qs.values_list(*value_names):
            system1ID = result[0]
            score1 = result[1]
            system2ID = result[2]
            score2 = result[3]
            start_time = result[4]
            end_time = result[5]
            duration = round(float(end_time) - float(start_time), 1)
            annotatorID = result[6]
            segmentID = result[7]
            marketID = '{0}-{1}'.format(result[8], result[9])
            domainName = result[10]
            itemType = result[11]
            taskID = result[12]
            campaignName = result[13]

            if annotatorID in user_data:
                username = user_data[annotatorID][0]
                useremail = user_data[annotatorID][1]
                usergroups = user_data[annotatorID][2]

            else:
                user = User.objects.get(pk=annotatorID)
                username = user.username
                useremail = user.email
                usergroups = ';'.join(
                    [
                        x.name
                        for x in user.groups.all()
                        if not x.name in LANGUAGE_CODES_AND_NAMES.keys()
                    ]
                )
                if not usergroups:
                    usergroups = 'NoGroupInfo'

                user_data[annotatorID] = (username, useremail, usergroups)

            system_scores[marketID + '-' + domainName].append(
                (
                    taskID,
                    segmentID,
                    username,
                    useremail,
                    usergroups,
                    system1ID,
                    score1,
                    system2ID,
                    score2,
                    start_time,
                    end_time,
                    duration,
                    itemType,
                    campaignName,
                )
            )

        # TODO: this is very intransparent... and needs to be fixed!
        x = system_scores
        s = [
            'taskID,segmentID,username,email,groups,system1ID,score1,system2ID,score2,startTime,endTime,durationInSeconds,itemType,campaignName'
        ]
        for l in x:
            for i in x[l]:
                s.append(','.join([str(a) for a in i]))

        from os.path import join
        from Appraise.settings import BASE_DIR

        media_file_path = join(BASE_DIR, 'media', csv_file)
        with open(media_file_path, 'w') as outfile:
            for c in s:
                outfile.write(c)
                outfile.write('\n')

    @classmethod
    def get_csv(cls, srcCode, tgtCode, domain):
        system_scores = defaultdict(list)
        qs = cls.objects.filter(completed=True)

        value_names = (
            'item__target1ID',
            'score1',
            'item__target2ID',
            'score2',
            'start_time',
            'end_time',
            'createdBy',
            'item__itemID',
            'item__metadata__market__sourceLanguageCode',
            'item__metadata__market__targetLanguageCode',
            'item__metadata__market__domainName',
            'item__itemType',
        )

        for result in qs.values_list(*value_names):
            if (
                not domain == result[10]
                or not srcCode == result[8]
                or not tgtCode == result[9]
            ):
                continue

            system1ID = result[0]
            score1 = result[1]
            system2ID = result[2]
            score2 = result[3]
            start_time = result[4]
            end_time = result[5]
            duration = round(float(end_time) - float(start_time), 1)
            annotatorID = result[6]
            segmentID = result[7]
            marketID = '{0}-{1}'.format(result[8], result[9])
            domainName = result[10]
            itemType = result[11]
            user = User.objects.get(pk=annotatorID)
            username = user.username
            useremail = user.email
            system_scores[marketID + '-' + domainName].append(
                (
                    segmentID,
                    username,
                    useremail,
                    system1ID,
                    score1,
                    system2ID,
                    score2,
                    duration,
                    itemType,
                )
            )

        return system_scores

    @classmethod
    def write_csv(cls, srcCode, tgtCode, domain, csvFile, allData=False):
        x = cls.get_csv(srcCode, tgtCode, domain)
        s = ['username,email,segmentID,score1,score2,durationInSeconds,itemType']
        if allData:
            s[0] = 'systemID,' + s[0]

        for l in x:
            for i in x[l]:
                e = i[1:] if not allData else i
                s.append(','.join([str(a) for a in e]))

        from os.path import join
        from Appraise.settings import BASE_DIR

        media_file_path = join(BASE_DIR, 'media', csvFile)
        with open(media_file_path, 'w') as outfile:
            for c in s:
                outfile.write(c)
                outfile.write('\n')

    @classmethod
    def get_system_scores(cls, campaign_id):
        system_scores = defaultdict(list)

        value_types = ('TGT', 'CHK')
        qs = cls.objects.filter(completed=True, item__itemType__in=value_types)

        # If campaign ID is given, only return results for this campaign.
        if campaign_id:
            qs = qs.filter(task__campaign__id=campaign_id)

        value_names = (
            'item__target1ID',
            'item__target2ID',
            'item__itemID',
            'score1',
            'score2',
        )
        for result in qs.values_list(*value_names):
            # if not result.completed or result.item.itemType not in ('TGT', 'CHK'):
            #    continue

            system1_ids = result[0].split('+')  # result.item.targetID.split('+')
            system2_ids = result[1].split('+')  # result.item.targetID.split('+')
            segment_id = result[2]
            score1 = result[3]  # .score
            score2 = result[4]  # .score

            for system_id in system1_ids:
                system_scores[system_id].append((segment_id, score1))
            for system_id in system2_ids:
                system_scores[system_id].append((segment_id, score2))

        return system_scores

    @classmethod
    def get_system_data(
        cls,
        campaign_id,
        extended_csv=False,
        expand_multi_sys=True,
        include_inactive=False,
        add_batch_info=False,
    ):
        item_types = ('TGT', 'CHK')
        if extended_csv:
            item_types += ('BAD', 'REF')

        qs = cls.objects.filter(completed=True, item__itemType__in=item_types)

        if campaign_id:
            qs = qs.filter(task__campaign__id=campaign_id)

        if not include_inactive:
            qs = qs.filter(createdBy__is_active=True)

        attributes_to_extract = (
            'item__segmentID',   # 0
            'createdBy__username',  # 1
            'item__target1ID',  # 2
            'item__target1Text',  # 3
            'item__target2ID',  # 4
            'item__target2Text', # 5
            'item__itemID',     # 6
            'item__itemType',   # 7
            'item__metadata__market__sourceLanguageCode',  # 8
            'item__metadata__market__targetLanguageCode',  # 9
            'score1',           # 10
            'score2',           # 11
            'selected_advantages',  # 12 (replacing selected_choices)
            'selected_advantages_other',  # 13 (new)
            'non_selected_problems',  # 14 (new)
            'non_selected_problems_other',  # 15 (new)
            'wiki_adequacy',  # 16 (new)
            'other_text',       # 17
            'span_diff_votes',                   # 18
            'span_diff_explanations',            # 19
            'span_diff_other_texts',             # 20
            'span_diff_texts',                  # 21
            'wikipedia_familiarity',             # 22
            'other_wikipedia_familiarity_text',  # 23
            'fluency_in_target_language',        # 24
            'feedback_options',                  # 25
            'other_feedback_options_text',       # 26
            'overallExperience',                 # 27
        )

        if extended_csv:
            attributes_to_extract += (
                'start_time',  # 28
                'end_time',    # 29
            )

        if add_batch_info:
            attributes_to_extract += (
                'task__batchNo',  # 30
                'item_id',        # 31
            )

        # --- HEADER
        header = (
            'segmentID',  # 0
            'annotator',  # 1
            'target1ID',  # 2
            'target1Text',  # 3
            'target2ID',  # 4
            'target2Text',  # 5
            'itemID',  # 6
            'itemType',  # 7
            'sourceLanguageCode',  # 8
            'targetLanguageCode',  # 9
            'score1',  # 10
            'score2',  # 11
            'selected_advantages',  # 12 (replacing selected_choices)
            'selected_advantages_other',  # 13 (new)
            'non_selected_problems',  # 14 (new)
            'non_selected_problems_other',  # 15 (new)
            'wiki_adequacy',  # 16 (new)
            'other_text',  # 17
            'span_diff_votes',  # 18
            'span_diff_explanations',  # 19
            'span_diff_other_texts',  # 20
            'span_diff_texts',  # 21
            'wikipedia_familiarity',  # 22
            'other_wikipedia_familiarity_text',  # 23
            'fluency_in_target_language',  # 24
            'feedback_options',  # 25
            'other_feedback_options_text',  # 26
            'overallExperience',  # 27
        )

        if extended_csv:
            header += (
                'start_time',  # 28
                'end_time',  # 29
            )

        if add_batch_info:
            header += (
                'batchNo',  # 30
                'item_id',  # 31
            )

        system_data = [header]

        # --- DATA
        for _result in qs.values_list(*attributes_to_extract):
            system_data.append([
                _result[0],  # segmentID
                _result[1],  # annotator
                _result[2],  # target1ID
                _result[3],  # target1Text
                _result[4],  # target2ID
                _result[5],  # target2Text
                _result[6],  # itemID
                _result[7],  # itemType
                _result[8],  # sourceLanguageCode
                _result[9],  # targetLanguageCode
                _result[10],  # score1
                _result[11],  # score2
                _result[12],  # selected_advantages
                _result[13],  # selected_advantages_other
                _result[14],  # non_selected_problems
                _result[15],  # non_selected_problems_other
                _result[16],  # wiki_adequacy
                _result[17],  # other_text
                _result[18],  # span_diff_votes
                _result[19],  # span_diff_explanations
                _result[20],  # span_diff_other_texts
                _result[21],  # span_diff_texts
                _result[22],  # wikipedia_familiarity
                _result[23],  # other_wikipedia_familiarity_text
                _result[24],  # fluency_in_target_language
                _result[25],  # feedback_options
                _result[26],  # other_feedback_options_text
                _result[27],  # overallExperience
            ])

            if extended_csv:
                system_data[-1].extend([
                    _result[28],  # start_time
                    _result[29],  # end_time
                ])

            if add_batch_info:
                system_data[-1].extend([
                    _result[30],  # batchNo
                    _result[31],  # item_id
                ])

        return system_data


    @classmethod
    def get_system_status(cls, campaign_id=None, sort_index=3):
        system_scores = cls.get_system_scores(campaign_id=None)
        non_english_codes = (
            'cs',
            'de',
            'fi',
            'lv',
            'tr',
            'tr',
            'ru',
            'zh',
        )

        codes = ['en-{0}'.format(x) for x in non_english_codes] + [
            '{0}-en'.format(x) for x in non_english_codes
        ]

        data = {}
        for code in codes:
            data[code] = {}
            for key in [x for x in system_scores if code in x]:
                data[code][key] = system_scores[key]

        output_data = {}
        for code in codes:
            total_annotations = sum([len(x) for x in data[code].values()])
            output_local = []
            for key in data[code]:
                x = data[code][key]
                z = sum(x) / total_annotations
                output_local.append((key, len(x), sum(x) / len(x), z))

            output_data[code] = list(
                sorted(output_local, key=lambda x: x[sort_index], reverse=True)
            )

        return output_data

    @classmethod
    def completed_results_for_user_and_campaign(cls, user, campaign):
        results = cls.objects.filter(
            activated=False,
            completed=True,
            createdBy=user,
            task__campaign=campaign,
        ).values_list('item_id', flat=True)

        return len(set(results))
