"""
Appraise evaluation framework

See LICENSE for usage details
"""
from datetime import datetime
from datetime import timezone
import logging
import json  # Import json module for JSON handling

utc = timezone.utc

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils.html import escape
from django.contrib import messages 
from django.http import HttpResponse, HttpResponseServerError
from django.template.loader import render_to_string

from Appraise.settings import BASE_CONTEXT
from Appraise.utils import _get_logger
from Campaign.models import Campaign
from Dashboard.models import SIGN_LANGUAGE_CODES
from EvalData.models import DataAssessmentResult
from EvalData.models import DataAssessmentTask
from EvalData.models import DirectAssessmentContextResult
from EvalData.models import DirectAssessmentContextTask
from EvalData.models import DirectAssessmentDocumentResult
from EvalData.models import DirectAssessmentDocumentTask
from EvalData.models import DirectAssessmentResult
from EvalData.models import DirectAssessmentTask
from EvalData.models import MultiModalAssessmentResult
from EvalData.models import MultiModalAssessmentTask
from EvalData.models import PairwiseAssessmentDocumentResult
from EvalData.models import PairwiseAssessmentDocumentTask
from EvalData.models import PairwiseAssessmentResult
from EvalData.models import PairwiseAssessmentTask
from EvalData.models import TaskAgenda

from difflib import SequenceMatcher # hiba added this to replicate the functionality of the target_texts_with_diffs function in ./EvalData/models/base_models.py:751

import re

def extract_marked_spans(candidate_text):
    return re.findall(r'<span class="diff[^"]*">(.*?)</span>', candidate_text)

# pylint: disable=import-error

LOGGER = _get_logger(name=__name__)

# pylint: disable=C0103,C0330

@login_required
def change_answers(request):
    """
    Reset the task agenda and redirect to the first task to allow users to change their answers.
    """
    # Import necessary models at the top of the function
    from EvalData.models import TaskAgenda, ObjectID, PairwiseAssessmentResult, PairwiseAssessmentTask
    logger = logging.getLogger(__name__)
    
    logger.info(f"change_answers called for user: {request.user.username}")
    
    # Find the user's task agenda for pairwise assessment
    agendas = TaskAgenda.objects.filter(user=request.user)
    logger.info(f"Found {agendas.count()} agendas for user")
    
    # Store a flag in the session to indicate we're in "edit mode"
    request.session['edit_mode'] = True
    # We want users to go through the introduction page, so don't set visited_introduction here
    
    # Check if user has completed any pairwise assessment tasks
    has_results = PairwiseAssessmentResult.objects.filter(
        createdBy=request.user,
        completed=True
    ).exists()
    logger.info(f"User has completed results: {has_results}")
    
    # If user has completed tasks but no agenda (already completed feedback),
    # we need to recreate the agenda with completed tasks moved back to open tasks
    if has_results and not agendas.exists():
        logger.info("User has results but no agenda, attempting to recreate")
        # Get the campaign from a completed result
        latest_result = PairwiseAssessmentResult.objects.filter(
            createdBy=request.user,
            completed=True
        ).order_by('-dateCompleted').first()
        
        if latest_result:
            campaign = latest_result.task.campaign
            logger.info(f"Found campaign: {campaign.campaignName}")
            
            # Create a new TaskAgenda for this user and campaign
            agenda = TaskAgenda.objects.create(user=request.user, campaign=campaign)
            logger.info(f"Created new agenda: {agenda}")
            
            # Get all completed tasks for this user and campaign
            tasks = set()
            task_results = PairwiseAssessmentResult.objects.filter(
                createdBy=request.user, 
                task__campaign=campaign
            ).values_list('task', flat=True).distinct()
            logger.info(f"Found {len(task_results)} distinct tasks")
            
            for result_id in task_results:
                try:
                    task_obj = PairwiseAssessmentTask.objects.get(id=result_id)
                    task_id = ObjectID.get_object_id(task_obj)
                    tasks.add(task_id)
                    logger.info(f"Added task ID: {task_id}")
                except PairwiseAssessmentTask.DoesNotExist:
                    # Skip tasks that don't exist anymore
                    logger.warning(f"Task with ID {result_id} does not exist")
                    continue
            
            # Add all tasks to the open_tasks list
            for task_id in tasks:
                agenda._open_tasks.add(task_id)
            
            agenda.save()
            logger.info(f"Saved agenda with {agenda._open_tasks.count()} open tasks")
            agendas = TaskAgenda.objects.filter(user=request.user)
    
    # If we still have no agendas, redirect to dashboard
    if not agendas.exists():
        logger.warning("No agendas found after recreation attempt")
        messages.error(request, "No previous tasks found to edit.")
        return redirect('dashboard')
    
    # We need to move tasks from completed to open, but preserve previous answers
    for agenda in agendas:
        # Get the campaign type to check if it's a pairwise assessment
        campaign_type = agenda.campaign.get_campaign_type()
        logger.info(f"Processing agenda with campaign type: {campaign_type}")
        
        if campaign_type == 'PairwiseAssessmentTask':
            # Get all task IDs from both open and completed tasks
            all_task_ids = []
            
            # Get completed tasks and move them back to open tasks
            completed_count = agenda._completed_tasks.count()
            logger.info(f"Found {completed_count} completed tasks to move back to open tasks")
            
            for task in list(agenda._completed_tasks.all()):
                agenda._open_tasks.add(task)
                agenda._completed_tasks.remove(task)
                
                # Get the actual task instance
                task_instance = task.get_object_instance()
                if task_instance:
                    all_task_ids.append(task_instance.id)
            
            # Also include any tasks that might already be in open_tasks
            for task in list(agenda._open_tasks.all()):
                task_instance = task.get_object_instance()
                if task_instance and task_instance.id not in all_task_ids:
                    all_task_ids.append(task_instance.id)
                    
            agenda.save()
            logger.info(f"After moving tasks: {agenda._open_tasks.count()} open tasks, {agenda._completed_tasks.count()} completed tasks")
            
            # Reset the completion status of all results for this user
            # This is the key change - we mark all results as incomplete so they can be edited
            if all_task_ids:
                results_updated = PairwiseAssessmentResult.objects.filter(
                    createdBy=request.user,
                    task__id__in=all_task_ids
                ).update(completed=False)
                logger.info(f"Reset completion status for {results_updated} results")
            
            # Store previous answers in session instead of deleting them
            # This will allow us to pre-populate the form when the user revisits each item
            if all_task_ids:
                previous_results = {}
                for result in PairwiseAssessmentResult.objects.filter(
                    createdBy=request.user,
                    task__id__in=all_task_ids
                ):
                    # Create a unique key for each item using task_id and item_id
                    key = f"{result.task.id}_{result.item.id}"
                    
                    # Process span diff votes
                    span_diff_votes = []
                    if result.span_diff_votes:
                        span_diff_votes = result.span_diff_votes.split(';\n')
                    
                    # Process span diff explanations
                    span_diff_explanations = []
                    if result.span_diff_explanations:
                        for exp_str in result.span_diff_explanations.split(';\n'):
                            if exp_str:
                                span_diff_explanations.append(exp_str.split(','))
                            else:
                                span_diff_explanations.append([])
                    
                    # Process span diff other texts
                    span_diff_other_texts = []
                    if result.span_diff_other_texts:
                        span_diff_other_texts = result.span_diff_other_texts.split(';\n')
                    
                    logger.info(f"Item {key}: span_diff_votes={span_diff_votes}, span_diff_explanations={span_diff_explanations}, span_diff_other_texts={span_diff_other_texts}")
                    
                    previous_results[key] = {
                        'score1': result.score1,
                        'score2': result.score2,
                        'selected_translation': result.selected_translation,
                        'selected_advantages': result.selected_advantages.split(';\n') if result.selected_advantages else [],
                        'selected_advantages_other': result.selected_advantages_other or '',
                        'non_selected_problems': result.non_selected_problems.split(';\n') if result.non_selected_problems else [],
                        'non_selected_problems_other': result.non_selected_problems_other or '',
                        'wiki_adequacy': result.wiki_adequacy.split(';\n') if result.wiki_adequacy else [],
                        'span_diff_votes': span_diff_votes,
                        'span_diff_explanations': span_diff_explanations,
                        'span_diff_other_texts': span_diff_other_texts
                    }
                
                # Store the previous results in the session
                request.session['previous_results'] = previous_results
                logger.info(f"Stored {len(previous_results)} previous results in session for pre-population")
            
            # Get the language code from the task
            try:
                task_instance = next(agenda.open_tasks(), None)
                if task_instance:
                    code = task_instance.marketTargetLanguageCode()
                    campaign_name = agenda.campaign.campaignName
                    logger.info(f"Found task with code: {code}, campaign: {campaign_name}")
                    
                    messages.success(request, "You can now change your previous answers.")
                    
                    # Redirect to the pairwise introduction page with the appropriate code and campaign
                    if code and campaign_name:
                        logger.info(f"Redirecting to pairwise-introduction with code: {code}, campaign: {campaign_name}")
                        return redirect('pairwise-introduction', code=code, campaign_name=campaign_name)
                    
                    logger.info("Redirecting to pairwise-introduction without code/campaign")
                    return redirect('pairwise-introduction')
                else:
                    logger.warning("No task instance found in open_tasks")
            except Exception as e:
                # Log the error and continue checking other agendas
                logger.error(f"Error processing task agenda: {e}", exc_info=True)
                continue
    
    logger.warning("No valid pairwise assessment tasks found")
    messages.error(request, "No previous tasks found to edit.")
    return redirect('dashboard')

@login_required
def direct_assessment(request, code=None, campaign_name=None):
    """
    Direct assessment annotation view.
    """
    t1 = datetime.now()

    campaign = None
    if campaign_name:
        campaign = Campaign.objects.filter(campaignName=campaign_name)
        if not campaign.exists():
            _msg = 'No campaign named "%s" exists, redirecting to dashboard'
            LOGGER.info(_msg, campaign_name)
            return redirect('dashboard')

        campaign = campaign[0]

    LOGGER.info(
        'Rendering direct assessment view for user "%s".',
        request.user.username or "Anonymous",
    )

    current_task = None

    # Try to identify TaskAgenda for current user.
    agendas = TaskAgenda.objects.filter(user=request.user)

    if campaign:
        agendas = agendas.filter(campaign=campaign)

    for agenda in agendas:
        LOGGER.info('Identified work agenda %s', agenda)

        tasks_to_complete = []
        for serialized_open_task in agenda.serialized_open_tasks():
            open_task = serialized_open_task.get_object_instance()

            # Skip tasks which are not available anymore
            if open_task is None:
                continue

            if open_task.next_item_for_user(request.user) is not None:
                current_task = open_task
                if not campaign:
                    campaign = agenda.campaign
            else:
                tasks_to_complete.append(serialized_open_task)

        modified = False
        for task in tasks_to_complete:
            modified = agenda.complete_open_task(task) or modified

        if modified:
            agenda.save()

    if not current_task and agendas.count() > 0:
        LOGGER.info('Work agendas completed, redirecting to dashboard')
        LOGGER.info('- code=%s, campaign=%s', code, campaign)
        return redirect('dashboard')

    # If language code has been given, find a free task and assign to user.
    if not current_task:
        current_task = DirectAssessmentTask.get_task_for_user(user=request.user)

    if not current_task:
        if code is None or campaign is None:
            LOGGER.info('No current task detected, redirecting to dashboard')
            LOGGER.info('- code=%s, campaign=%s', code, campaign)
            return redirect('dashboard')

        LOGGER.info(
            'Identifying next task for code "%s", campaign="%s"',
            code,
            campaign,
        )
        next_task = DirectAssessmentTask.get_next_free_task_for_language(
            code, campaign, request.user
        )

        if next_task is None:
            LOGGER.info('No next task detected, redirecting to dashboard')
            return redirect('dashboard')

        next_task.assignedTo.add(request.user)
        next_task.save()

        current_task = next_task

    if current_task:
        if not campaign:
            campaign = current_task.campaign

        elif campaign.campaignName != current_task.campaign.campaignName:
            _msg = 'Incompatible campaign given, using item campaign instead!'
            LOGGER.info(_msg)
            campaign = current_task.campaign

    t2 = datetime.now()
    if request.method == "POST":
        score = request.POST.get('score', None)
        item_id = request.POST.get('item_id', None)
        task_id = request.POST.get('task_id', None)
        start_timestamp = request.POST.get('start_timestamp', None)
        end_timestamp = request.POST.get('end_timestamp', None)

        LOGGER.info(f'score={score}, item_id={item_id}')
        if not score or score == -1:
            LOGGER.debug(f"Score not submitted ({score}).")

        if score and item_id and start_timestamp and end_timestamp:
            duration = float(end_timestamp) - float(start_timestamp)
            LOGGER.debug(float(start_timestamp))
            LOGGER.debug(float(end_timestamp))
            LOGGER.info(
                f'start={start_timestamp,}, end={end_timestamp}, duration={duration}',
            )

            current_item = current_task.next_item_for_user(request.user)
            if current_item.itemID != int(item_id) or current_item.id != int(task_id):
                LOGGER.debug(
                    f'Item ID {item_id} does not match item {current_item.itemID}, will not save!'
                )
            else:
                utc_now = datetime.utcnow().replace(tzinfo=utc)
                # pylint: disable=E1101
                DirectAssessmentResult.objects.create(
                    score=score,
                    start_time=float(start_timestamp),
                    end_time=float(end_timestamp),
                    item=current_item,
                    task=current_task,
                    createdBy=request.user,
                    activated=False,
                    completed=True,
                    dateCompleted=utc_now,
                )

    t3 = datetime.now()

    current_item, completed_items = current_task.next_item_for_user(
        request.user, return_completed_items=True
    )
    if not current_item:
        LOGGER.info('No current item detected, redirecting to dashboard')
        return redirect('dashboard')

    # completed_items_check = current_task.completed_items_for_user(
    #     request.user)
    completed_blocks = int(completed_items / 10)
    _msg = 'completed_items=%s, completed_blocks=%s'
    LOGGER.info(_msg, completed_items, completed_blocks)

    source_language = current_task.marketSourceLanguage()
    target_language = current_task.marketTargetLanguage()

    t4 = datetime.now()

    # Define priming question
    #
    # Default:
    #   How accurately does the above candidate text convey the original
    #   semantics of the source text? Slider ranges from
    #   <em>Not at all</em> (left) to <em>Perfectly</em> (right).
    #
    # We currently allow specific overrides, based on campaign name.
    reference_label = 'Source text'
    candidate_label = 'Candidate translation'
    priming_question_text = (
        'How accurately does the above candidate text convey the original '
        'semantics of the source text? Slider ranges from '
        '<em>Not at all</em> (left) to <em>Perfectly</em> (right).'
    )

    _reference_campaigns = ('HumanEvalFY19{0}'.format(x) for x in ('7B',))

    _adequacy_campaigns = ('HumanEvalFY19{0}'.format(x) for x in ('51', '57', '63'))

    _fluency_campaigns = ('HumanEvalFY19{0}'.format(x) for x in ('52', '58', '64'))

    if campaign.campaignName in _reference_campaigns:
        reference_label = 'Reference text'
        candidate_label = 'Candidate translation'
        priming_question_text = (
            'How accurately does the above candidate text convey the original '
            'semantics of the reference text? Slider ranges from '
            '<em>Not at all</em> (left) to <em>Perfectly</em> (right).'
        )

    elif campaign.campaignName in _adequacy_campaigns:
        reference_label = 'Candidate A'
        candidate_label = 'Candidate B'
        priming_question_text = (
            'How accurately does candidate text B convey the original '
            'semantics of candidate text A? Slider ranges from '
            '<em>Not at all</em> (left) to <em>Perfectly</em> (right).'
        )

    elif campaign.campaignName in _fluency_campaigns:
        reference_label = 'Candidate A'
        candidate_label = 'Candidate B'
        priming_question_text = (
            'Which of the two candidate texts is more fluent? Slider marks '
            'preference for <em>Candidate A</em> (left), no difference '
            '(middle) or preference for <em>Candidate B</em> (right).'
        )

    campaign_opts = set((campaign.campaignOptions or "").lower().split(";"))

    if 'sqm' in campaign_opts:
        html_file = 'EvalView/direct-assessment-sqm.html'
    else:
        html_file = 'EvalView/direct-assessment-context.html'

    if 'namedentit' in campaign_opts:
        html_file = 'EvalView/direct-assessment-named-entities.html'

    if 'reference' in campaign_opts:
        reference_label = 'Reference text in {}'.format(target_language)
        candidate_label = 'Candidate translation in {}'.format(target_language)

    context = {
        'active_page': 'direct-assessment',
        'reference_label': reference_label,
        'reference_text': current_item.sourceText,
        'candidate_label': candidate_label,
        'candidate_text': current_item.targetText,
        'priming_question_text': priming_question_text,
        'item_id': current_item.itemID,
        'task_id': current_item.id,
        'completed_blocks': completed_blocks,
        'items_left_in_block': 10 - (completed_items - completed_blocks * 10),
        'source_language': source_language,
        'target_language': target_language,
        'debug_times': (t2 - t1, t3 - t2, t4 - t3, t4 - t1),
        'template_debug': 'debug' in request.GET,
        'campaign': campaign.campaignName,
        'datask_id': current_task.id,
        'trusted_user': current_task.is_trusted_user(request.user),
    }
    context.update(BASE_CONTEXT)

    return render(request, html_file, context)


# pylint: disable=C0103,C0330
@login_required
def direct_assessment_context(request, code=None, campaign_name=None):
    """
    Direct assessment context annotation view.
    """
    t1 = datetime.now()

    campaign = None
    if campaign_name:
        campaign = Campaign.objects.filter(campaignName=campaign_name)
        if not campaign.exists():
            _msg = 'No campaign named "%s" exists, redirecting to dashboard'
            LOGGER.info(_msg, campaign_name)
            return redirect('dashboard')

        campaign = campaign[0]

    LOGGER.info(
        'Rendering direct assessment context view for user "%s".',
        request.user.username or "Anonymous",
    )

    current_task = None

    # Try to identify TaskAgenda for current user.
    agendas = TaskAgenda.objects.filter(user=request.user)

    if campaign:
        agendas = agendas.filter(campaign=campaign)

    for agenda in agendas:
        LOGGER.info('Identified work agenda %s', agenda)

        tasks_to_complete = []
        for serialized_open_task in agenda.serialized_open_tasks():
            open_task = serialized_open_task.get_object_instance()

            # Skip tasks which are not available anymore
            if open_task is None:
                continue

            if open_task.next_item_for_user(request.user) is not None:
                current_task = open_task
                if not campaign:
                    campaign = agenda.campaign
            else:
                tasks_to_complete.append(serialized_open_task)

        modified = False
        for task in tasks_to_complete:
            modified = agenda.complete_open_task(task) or modified

        if modified:
            agenda.save()

    if not current_task and agendas.count() > 0:
        LOGGER.info('Work agendas completed, redirecting to dashboard')
        LOGGER.info('- code=%s, campaign=%s', code, campaign)
        return redirect('dashboard')

    # If language code has been given, find a free task and assign to user.
    if not current_task:
        current_task = DirectAssessmentContextTask.get_task_for_user(user=request.user)

    if not current_task:
        if code is None or campaign is None:
            LOGGER.info('No current task detected, redirecting to dashboard')
            LOGGER.info('- code=%s, campaign=%s', code, campaign)
            return redirect('dashboard')

        LOGGER.info(
            'Identifying next task for code "%s", campaign="%s"',
            code,
            campaign,
        )
        next_task = DirectAssessmentContextTask.get_next_free_task_for_language(
            code, campaign, request.user
        )

        if next_task is None:
            LOGGER.info('No next task detected, redirecting to dashboard')
            return redirect('dashboard')

        next_task.assignedTo.add(request.user)
        next_task.save()

        current_task = next_task

    if current_task:
        if not campaign:
            campaign = current_task.campaign

        elif campaign.campaignName != current_task.campaign.campaignName:
            _msg = 'Incompatible campaign given, using item campaign instead!'
            LOGGER.info(_msg)
            campaign = current_task.campaign

    t2 = datetime.now()
    if request.method == "POST":
        score = request.POST.get('score', None)
        item_id = request.POST.get('item_id', None)
        task_id = request.POST.get('task_id', None)
        document_id = request.POST.get('document_id', None)
        start_timestamp = request.POST.get('start_timestamp', None)
        end_timestamp = request.POST.get('end_timestamp', None)
        LOGGER.info('score=%s, item_id=%s', score, item_id)
        if score and item_id and start_timestamp and end_timestamp:
            duration = float(end_timestamp) - float(start_timestamp)
            LOGGER.debug(float(start_timestamp))
            LOGGER.debug(float(end_timestamp))
            LOGGER.info(
                'start=%s, end=%s, duration=%s',
                start_timestamp,
                end_timestamp,
                duration,
            )
            current_item = current_task.next_item_for_user(request.user)
            if (
                current_item.itemID != int(item_id)
                or current_item.id != int(task_id)
                or current_item.documentID != document_id
            ):
                _msg = 'Item ID %s does not match item %s, will not save!'
                LOGGER.debug(_msg, item_id, current_item.itemID)

            else:
                utc_now = datetime.utcnow().replace(tzinfo=utc)
                # pylint: disable=E1101
                DirectAssessmentContextResult.objects.create(
                    score=score,
                    start_time=float(start_timestamp),
                    end_time=float(end_timestamp),
                    item=current_item,
                    task=current_task,
                    createdBy=request.user,
                    activated=False,
                    completed=True,
                    dateCompleted=utc_now,
                )

    t3 = datetime.now()

    current_item, completed_items = current_task.next_item_for_user(
        request.user, return_completed_items=True
    )
    if not current_item:
        LOGGER.info('No current item detected, redirecting to dashboard')
        return redirect('dashboard')

    # completed_items_check = current_task.completed_items_for_user(
    #     request.user)
    completed_blocks = int(completed_items / 10)
    _msg = 'completed_items=%s, completed_blocks=%s'
    LOGGER.info(_msg, completed_items, completed_blocks)

    source_language = current_task.marketSourceLanguage()
    target_language = current_task.marketTargetLanguage()

    t4 = datetime.now()

    # Define priming question
    #
    # Default:
    #   How accurately does the above candidate text convey the original
    #   semantics of the source text? Slider ranges from
    #   <em>Not at all</em> (left) to <em>Perfectly</em> (right).
    #
    # We currently allow specific overrides, based on campaign name.
    reference_label = 'Source text'
    candidate_label = 'Candidate translation'
    priming_question_text = (
        'How accurately does the above candidate text convey the original '
        'semantics of the source text? Slider ranges from '
        '<em>Not at all</em> (left) to <em>Perfectly</em> (right).'
    )

    if current_item.isCompleteDocument:
        priming_question_text = (
            'How accurately does the above candidate document convey the '
            'original semantics of the source document? Slider ranges from '
            '<em>Not at all</em> (left) to <em>Perfectly</em> (right).'
        )

    _reference_campaigns = ('HumanEvalFY19{0}'.format(x) for x in ('7B',))

    _adequacy_campaigns = ('HumanEvalFY19{0}'.format(x) for x in ('51', '57', '63'))

    _fluency_campaigns = ('HumanEvalFY19{0}'.format(x) for x in ('52', '58', '64'))

    if campaign.campaignName in _reference_campaigns:
        reference_label = 'Reference text'
        candidate_label = 'Candidate translation'
        priming_question_text = (
            'How accurately does the above candidate text convey the original '
            'semantics of the reference text? Slider ranges from '
            '<em>Not at all</em> (left) to <em>Perfectly</em> (right).'
        )

    elif campaign.campaignName in _adequacy_campaigns:
        reference_label = 'Candidate A'
        candidate_label = 'Candidate B'
        priming_question_text = (
            'How accurately does candidate text B convey the original '
            'semantics of candidate text A? Slider ranges from '
            '<em>Not at all</em> (left) to <em>Perfectly</em> (right).'
        )

    elif campaign.campaignName in _fluency_campaigns:
        reference_label = 'Candidate A'
        candidate_label = 'Candidate B'
        priming_question_text = (
            'Which of the two candidate texts is more fluent? Slider marks '
            'preference for <em>Candidate A</em> (left), no difference '
            '(middle) or preference for <em>Candidate B</em> (right).'
        )
    context = {
        'active_page': 'direct-assessment',
        'reference_label': reference_label,
        'reference_text': current_item.sourceText,
        'reference_context_left': None,  # current_item.sourceContextLeft,
        'reference_context_right': None,  # current_item.sourceContextRight,
        'candidate_label': candidate_label,
        'candidate_text': current_item.targetText,
        'candidate_context_left': None,  # current_item.targetContextLeft,
        'candidate_context_right': None,  # current_item.targetContextRight,
        'priming_question_text': priming_question_text,
        'item_id': current_item.itemID,
        'task_id': current_item.id,
        'document_id': current_item.documentID,
        'isCompleteDocument': current_item.isCompleteDocument,
        'completed_blocks': completed_blocks,
        'items_left_in_block': 10 - (completed_items - completed_blocks * 10),
        'source_language': source_language,
        'target_language': target_language,
        'debug_times': (t2 - t1, t3 - t2, t4 - t3, t4 - t1),
        'template_debug': 'debug' in request.GET,
        'campaign': campaign.campaignName,
        'datask_id': current_task.id,
        'trusted_user': current_task.is_trusted_user(request.user),
    }
    context.update(BASE_CONTEXT)

    return render(request, 'EvalView/direct-assessment-context.html', context)


# pylint: disable=C0103,C0330
@login_required
def direct_assessment_document(request, code=None, campaign_name=None):
    """
    Direct assessment document annotation view.
    """

    t1 = datetime.now()

    campaign = None
    if campaign_name:
        campaign = Campaign.objects.filter(campaignName=campaign_name)
        if not campaign.exists():
            _msg = 'No campaign named "%s" exists, redirecting to dashboard'
            LOGGER.info(_msg, campaign_name)
            return redirect('dashboard')

        campaign = campaign[0]

    LOGGER.info(
        'Rendering direct assessment document view for user "%s".',
        request.user.username or "Anonymous",
    )

    current_task = None

    # Try to identify TaskAgenda for current user.
    agendas = TaskAgenda.objects.filter(user=request.user)

    if campaign:
        agendas = agendas.filter(campaign=campaign)

    for agenda in agendas:
        LOGGER.info('Identified work agenda %s', agenda)

        tasks_to_complete = []
        for serialized_open_task in agenda.serialized_open_tasks():
            open_task = serialized_open_task.get_object_instance()

            # Skip tasks which are not available anymore
            if open_task is None:
                continue

            if open_task.next_item_for_user(request.user) is not None:
                current_task = open_task
                if not campaign:
                    campaign = agenda.campaign
            else:
                tasks_to_complete.append(serialized_open_task)

        modified = False
        for task in tasks_to_complete:
            modified = agenda.complete_open_task(task) or modified

        if modified:
            agenda.save()

    if not current_task and agendas.count() > 0:
        LOGGER.info('Work agendas completed, redirecting to dashboard')
        LOGGER.info('- code=%s, campaign=%s', code, campaign)
        return redirect('dashboard')

    # If language code has been given, find a free task and assign to user.
    if not current_task:
        current_task = DirectAssessmentDocumentTask.get_task_for_user(user=request.user)

    if not current_task:
        if code is None or campaign is None:
            LOGGER.info('No current task detected, redirecting to dashboard')
            LOGGER.info('- code=%s, campaign=%s', code, campaign)
            return redirect('dashboard')

        LOGGER.info(
            'Identifying next task for code "%s", campaign="%s"',
            code,
            campaign,
        )
        next_task = DirectAssessmentDocumentTask.get_next_free_task_for_language(
            code, campaign, request.user
        )

        if next_task is None:
            LOGGER.info('No next task detected, redirecting to dashboard')
            return redirect('dashboard')

        next_task.assignedTo.add(request.user)
        next_task.save()

        current_task = next_task

    if current_task:
        if not campaign:
            campaign = current_task.campaign

        elif campaign.campaignName != current_task.campaign.campaignName:
            _msg = 'Incompatible campaign given, using item campaign instead!'
            LOGGER.info(_msg)
            campaign = current_task.campaign

    # hijack this function if it uses MQM
    campaign_opts = set((campaign.campaignOptions or "").lower().split(";"))
    if 'mqm' in campaign_opts or 'esa' in campaign_opts:
        return direct_assessment_document_mqmesa(campaign, current_task, request)

    # Handling POST requests differs from the original direct_assessment/
    # direct_assessment_context view, but the input is the same: a score for the
    # single submitted item
    t2 = datetime.now()
    ajax = False
    item_saved = False
    error_msg = ''
    if request.method == "POST":
        score = request.POST.get('score', None)
        item_id = request.POST.get('item_id', None)
        task_id = request.POST.get('task_id', None)
        document_id = request.POST.get('document_id', None)
        start_timestamp = request.POST.get('start_timestamp', None)
        end_timestamp = request.POST.get('end_timestamp', None)
        ajax = bool(request.POST.get('ajax', None) == 'True')

        LOGGER.info('score=%s, item_id=%s', score, item_id)
        print(f'Got request score={score}, item_id={item_id}, ajax={ajax}')

        # If all required information was provided in the POST request
        if score and item_id and start_timestamp and end_timestamp:
            duration = float(end_timestamp) - float(start_timestamp)
            LOGGER.debug(float(start_timestamp))
            LOGGER.debug(float(end_timestamp))
            LOGGER.info(
                'start=%s, end=%s, duration=%s',
                start_timestamp,
                end_timestamp,
                duration,
            )

            # Get all items from the document that the submitted item belongs
            # to, and all already collected scores for this document
            (
                current_item,
                block_items,
                block_results,
            ) = current_task.next_document_for_user(
                request.user, return_statistics=False
            )

            # An item from the right document was submitted
            if current_item.documentID == document_id:
                # This is the item that we expected to be annotated first,
                # which means that there is no score for the current item, so
                # create new score
                if current_item.itemID == int(item_id) and current_item.id == int(
                    task_id
                ):
                    utc_now = datetime.utcnow().replace(tzinfo=utc)
                    # pylint: disable=E1101
                    DirectAssessmentDocumentResult.objects.create(
                        score=score,
                        start_time=float(start_timestamp),
                        end_time=float(end_timestamp),
                        item=current_item,
                        task=current_task,
                        createdBy=request.user,
                        activated=False,
                        completed=True,
                        dateCompleted=utc_now,
                    )
                    print('Item {} (itemID={}) saved'.format(task_id, item_id))
                    item_saved = True

                # It is not the current item, so check if the result for it
                # exists
                else:
                    # Check if there is a score result for the submitted item
                    # TODO: this could be a single query, would it be better or
                    # more effective?
                    current_result = None
                    for result in block_results:
                        if not result:
                            continue
                        if result.item.itemID == int(item_id) and result.item.id == int(
                            task_id
                        ):
                            current_result = result
                            break

                    # If already scored, update the result
                    # TODO: consider adding new score, not updating the
                    # previous one
                    if current_result:
                        prev_score = current_result.score
                        current_result.score = score
                        current_result.start_time = float(start_timestamp)
                        current_result.end_time = float(end_timestamp)
                        utc_now = datetime.utcnow().replace(tzinfo=utc)
                        current_result.dateCompleted = utc_now
                        current_result.save()
                        _msg = 'Item {} (itemID={}) updated {}->{}'.format(
                            task_id, item_id, prev_score, score
                        )
                        LOGGER.debug(_msg)
                        print(_msg)
                        item_saved = True

                    # If not yet scored, check if the submitted item is from
                    # the expected document. Note that document ID is **not**
                    # sufficient, because there can be multiple documents with
                    # the same ID in the task.
                    else:
                        found_item = False
                        for item in block_items:
                            if item.itemID == int(item_id) and item.id == int(task_id):
                                found_item = item
                                break

                        # The submitted item is from the same document as the
                        # first unannotated item. It is fine, so save it
                        if found_item:
                            utc_now = datetime.utcnow().replace(tzinfo=utc)
                            # pylint: disable=E1101
                            DirectAssessmentDocumentResult.objects.create(
                                score=score,
                                start_time=float(start_timestamp),
                                end_time=float(end_timestamp),
                                item=found_item,
                                task=current_task,
                                createdBy=request.user,
                                activated=False,
                                completed=True,
                                dateCompleted=utc_now,
                            )
                            _msg = 'Item {} (itemID={}) saved, although it was not the next item'.format(
                                task_id, item_id
                            )
                            LOGGER.debug(_msg)
                            print(_msg)
                            item_saved = True

                        else:
                            error_msg = (
                                'We did not expect this item to be submitted. '
                                'If you used backward/forward buttons in your browser, '
                                'please reload the page and try again.'
                            )

                            _msg = 'Item ID {} does not match item {}, will not save!'.format(
                                item_id, current_item.itemID
                            )
                            LOGGER.debug(_msg)
                            print(_msg)

            # An item from a wrong document was submitted
            else:
                print(
                    'Different document IDs: {} != {}, will not save!'.format(
                        current_item.documentID, document_id
                    )
                )

                error_msg = (
                    'We did not expect an item from this document to be submitted. '
                    'If you used backward/forward buttons in your browser, '
                    'please reload the page and try again.'
                )

    t3 = datetime.now()

    # Get all items from the document that the first unannotated item in the
    # task belongs to, and collect some additional statistics
    (
        current_item,
        completed_items,
        completed_blocks,
        completed_items_in_block,
        block_items,
        block_results,
        total_blocks,
    ) = current_task.next_document_for_user(request.user)

    if not current_item:
        LOGGER.info('No current item detected, redirecting to dashboard')
        return redirect('dashboard')

    # Get item scores from the latest corresponding results
    block_scores = []
    _prev_item = None
    for item, result in zip(block_items, block_results):
        item_scores = {
            'completed': bool(result and result.score > -1),
            'current_item': bool(item.id == current_item.id),
            'score': result.score if result else -1,
        }

        # This is a hot fix for a bug in the IWSLT2022 Isometric Task batches,
        # where the document ID wasn't correctly incremented.
        # TODO: delete after the campaign is finished or fix all documents in DB
        if (
            'iwslt2022isometric' in campaign_opts
            and item.isCompleteDocument
            and item.itemID != (_prev_item.itemID + 1)
        ):
            item.itemID += 1
            item.save()
            _msg = 'Self-repaired the document item {} for user {}'.format(
                item, request.user.username
            )
            print(_msg)
            LOGGER.info(_msg)

        block_scores.append(item_scores)
        _prev_item = item

    # completed_items_check = current_task.completed_items_for_user(
    #     request.user)
    _msg = 'completed_items=%s, completed_blocks=%s'
    LOGGER.info(_msg, completed_items, completed_blocks)

    source_language = current_task.marketSourceLanguage()
    target_language = current_task.marketTargetLanguage()

    t4 = datetime.now()

    # By default, source and target items are text segments
    source_item_type = 'text'
    target_item_type = 'text'
    reference_label = 'Source text'
    candidate_label = 'Candidate translation'

    monolingual_task = 'monolingual' in campaign_opts
    sign_translation = 'signlt' in campaign_opts
    speech_translation = 'speechtranslation' in campaign_opts
    static_context = 'staticcontext' in campaign_opts
    use_sqm = 'sqm' in campaign_opts
    ui_language = 'enu'
    doc_guidelines = 'doclvlguideline' in campaign_opts

    error_types = None
    critical_error = None

    if 'wmt22signlt' in campaign_opts:
        sign_translation = True
        use_sqm = True
        ui_language = 'deu'

    if sign_translation:
        # For sign languages, source or target segments are videos
        if source_language in SIGN_LANGUAGE_CODES:
            source_item_type = 'video'
            reference_label = 'Source video'
        if target_language in SIGN_LANGUAGE_CODES:
            target_item_type = 'video'
            candidate_label = 'Candidate translation (video)'
        else:
            sign_translation = False  # disable sign-specific SQM instructions

    priming_question_texts = [
        'Below you see a document with {0} sentences in {1} (left columns) '
        'and their corresponding candidate translations in {2} (right columns). '
        'Score each candidate sentence translation in the document context. '
        'You may revisit already scored sentences and update their scores at any time '
        'by clicking at a source {3}.'.format(
            len(block_items) - 1,
            source_language,
            target_language,
            source_item_type,
        ),
        'Assess the translation quality answering the question: ',
        'How accurately does the candidate text (right column, in bold) convey the '
        'original semantics of the source text (left column) in the document context? ',
    ]
    document_question_texts = [
        'Please score the overall document translation quality (you can score '
        'the whole document only after scoring all individual sentences first).',
        'Assess the translation quality answering the question: ',
        'How accurately does the <strong>entire</strong> candidate document translation '
        'in {0} (right column) convey the original semantics of the source document '
        'in {1} (left column)? '.format(target_language, source_language),
    ]

    if use_sqm:
        priming_question_texts = priming_question_texts[:1]
        document_question_texts = document_question_texts[:1]

    if monolingual_task:
        source_language = None
        priming_question_texts = [
            'Below you see a document with {0} sentences in {1}. '
            'Score each sentence in the document context. '
            'You may revisit already scored sentences and update their scores at any time '
            'by clicking at a source text.'.format(
                len(block_items) - 1, target_language
            ),
        ]
        document_question_texts = [
            'Please score the overall document quality (you can score '
            'the whole document only after scoring all individual sentences first).',
        ]
        candidate_label = None

    if doc_guidelines:
        priming_question_texts = [
            'Below you see a document with {0} partial paragraphs in {1} (left columns) '
            'and their corresponding two candidate translations in {2} (middle and right column). '
            'Please score each paragraph of both candidate translations '
            '<u><b>paying special attention to document-level properties, '
            'such as consistency of style, selection of translation terms, formality, '
            'and so on</b></u>, in addition to the usual correctness criteria. '
            'Note that sentences in each paragraph may be separated by <i>&lt;eos&gt;</i> tags '
            'for convenience. These tags, if present, should not impact your assessment. '.format(
                len(block_items) - 1,
                source_language,
                target_language,
            ),
        ]

    # German instructions for WMT22 sign language task
    if 'wmt22signlt' in campaign_opts:
        if 'text2sign' in campaign_opts:
            priming_question_texts = [
                'Unten sehen Sie ein Dokument mit {0} Sätzen auf Deutsch (linke Spalten) '
                'und die entsprechenden möglichen Übersetzungen in Deutschschweizer '
                'Gebärdensprache (DSGS) (rechte Spalten). Bewerten Sie jede mögliche '
                'Übersetzung des Satzes im Kontext des Dokuments. '
                'Sie können bereits bewertete Sätze jederzeit durch Anklicken eines '
                'Quelltextes erneut aufrufen und die Bewertung aktualisieren.'.format(
                    len(block_items) - 1,
                ),
            ]
        elif 'sign2text-seglvl' in campaign_opts:
            priming_question_texts = [
                'Unten sehen Sie ein Set von {0} unzusammenhängenden Sätzen in Deutschschweizer '
                'Gebärdensprache (DSGS) (linke Spalten) und die entsprechenden möglichen '
                'Übersetzungen auf Deutsch (rechte Spalten). '
                'Sie können bereits bewertete Sätze jederzeit durch Anklicken eines '
                'Eingabevideos erneut aufrufen und die Bewertung aktualisieren.'.format(
                    len(block_items) - 1,
                ),
            ]
        else:
            priming_question_texts = [
                'Unten sehen Sie ein Dokument mit {0} Sätzen in Deutschschweizer '
                'Gebärdensprache (DSGS) (linke Spalten) und die entsprechenden möglichen '
                'Übersetzungen auf Deutsch (rechte Spalten). Bewerten Sie jede mögliche '
                'Übersetzung des Satzes im Kontext des Dokuments. '
                'Sie können bereits bewertete Sätze jederzeit durch Anklicken eines '
                'Eingabevideos erneut aufrufen und die Bewertung aktualisieren.'.format(
                    len(block_items) - 1,
                ),
            ]
        document_question_texts = [
            'Bitte bewerten Sie die Übersetzungsqualität des gesamten Dokuments. '
            '(Sie können das Dokument erst bewerten, nachdem Sie zuvor alle Sätze '
            'einzeln bewertet haben.)',
        ]

    # Special instructions for IWSLT 2022 dialect task
    if 'iwslt2022dialectsrc' in campaign_opts:
        speech_translation = True
        priming_question_texts += [
            'Please take into consideration the following aspects when assessing the translation quality:',
            '<ul>'
            '<li>The document is part of a conversation thread between two speakers, '
            'and each segment starts with either "A:" or "B:" to indicate the '
            'speaker identity.</li>'
            '<li>Some candidate translations may contain "%pw" or "% pw", but since they correspond to '
            'partial words in the speech they should not be considered as errors during evaluation.</li>'
            '<li>Please ignore the lack of capitalization and punctuation. Also, '
            'please ignore "incorrect" grammar and focus more on the meaning: '
            'these segments are informal conversations, so grammatical rules are '
            'not so strict.</li>',
        ]
        if current_task.marketSourceLanguageCode() == "aeb":
            priming_question_texts[-1] += (
                '<li>The original source is Tunisian Arabic speech. '
                + 'There may be some variation in the transcription.</li>'
            )
        priming_question_texts[-1] += '</ul>'

    # Special instructions for IWSLT 2022 isometric task
    if 'iwslt2022isometric' in campaign_opts:
        priming_question_texts += [
            'Please take into consideration the following aspects when assessing the translation quality:',
            '<ul>'
            '<li>The source texts come from transcribed video content published on YouTube.</li>'
            '<li>Transcribed sentences have been split into segments based on pauses in the audio. '
            'It may happen that a single source sentence is split into multiple segments.</li>'
            '<li>Please score each segment (including very short segments) individually with regard to '
            'the source segment and the surrounding context.</li>'
            '<li>Take into account both grammar and meaning when scoring the segments.</li>'
            '<li>Please pay attention to issues like repeated or new content in the candidate '
            'translation, which is not present in the source text.</li>'
            '</ul>',
        ]

    # A part of context used in responses to both Ajax and standard POST
    # requests
    context = {
        'active_page': 'direct-assessment-document',
        'item_id': current_item.itemID,
        'task_id': current_item.id,
        'document_id': current_item.documentID,
        'completed_blocks': completed_blocks,
        'total_blocks': total_blocks,
        'items_left_in_block': len(block_items) - completed_items_in_block,
        'source_language': source_language,
        'target_language': target_language,
        'source_item_type': source_item_type,
        'target_item_type': target_item_type,
        'debug_times': (t2 - t1, t3 - t2, t4 - t3, t4 - t1),
        'template_debug': 'debug' in request.GET,
        'campaign': campaign.campaignName,
        'datask_id': current_task.id,
        'trusted_user': current_task.is_trusted_user(request.user),
        # Task variations
        'errortypes': error_types,
        'criticalerror': critical_error,
        'monolingual': monolingual_task,
        'signlt': sign_translation,
        'speech': speech_translation,
        'static_context': static_context,
        'sqm': use_sqm,
        'ui_lang': ui_language,
    }

    if ajax:
        ajax_context = {'saved': item_saved, 'error_msg': error_msg}
        context.update(ajax_context)
        context.update(BASE_CONTEXT)
        return JsonResponse(context)  # Sent response to the Ajax POST request

    page_context = {
        'items': zip(block_items, block_scores),
        'reference_label': reference_label,
        'candidate_label': candidate_label,
        'priming_question_texts': priming_question_texts,
        'document_question_texts': document_question_texts,
    }
    context.update(page_context)
    context.update(BASE_CONTEXT)

    return render(request, 'EvalView/direct-assessment-document.html', context)


def direct_assessment_document_mqmesa(campaign, current_task, request):
    """
    Direct assessment document annotation view with MQM/ESA.
    """
    campaign_opts = set((campaign.campaignOptions or "").lower().split(";"))

    # POST means that we want to store
    if request.method == "POST":
        score = request.POST.get('score', None)
        mqm = request.POST.get('mqm', None)
        item_id = request.POST.get('item_id', None)
        task_id = request.POST.get('task_id', None)
        start_timestamp = request.POST.get('start_timestamp', None)
        end_timestamp = request.POST.get('end_timestamp', None)
        ajax = bool(request.POST.get('ajax', None) == 'True')


        db_item = current_task.items.filter(
            itemID=item_id,
            id=task_id,
        )


        if len(db_item) == 0:
            error_msg = (
                f'We could not find item {item_id} in task {task_id}.'
            )
            LOGGER.error(error_msg)
            item_saved = False
        elif len(db_item) > 1:
            error_msg = (
                f'Found more than one item {item_id} in task {task_id}.'
                'This is from incorrectly set up batches'
            )
            LOGGER.error(error_msg)
            item_saved = False
        else:
            DirectAssessmentDocumentResult.objects.create(
                score=score,
                mqm=mqm,
                start_time=float(start_timestamp),
                end_time=float(end_timestamp),
                item=list(db_item)[0],
                task=current_task,
                createdBy=request.user,
                activated=False,
                completed=True,
                dateCompleted=datetime.utcnow().replace(tzinfo=utc),
            )
            error_msg = f'Item {task_id} (itemID={item_id}) saved'
            LOGGER.info(error_msg)
            item_saved = True

        LOGGER.info(f'score={score}, item_id={item_id}, mqm={mqm}')
        print(f'Got request score={score}, item_id={item_id}, ajax={ajax}, mqm={mqm}')
    else:
        ajax = False

    # Get all items from the document that the first unannotated item in the
    # task belongs to, and collect some additional statistics
    (
        next_item,
        items_completed,
        docs_completed,
        doc_items,
        doc_items_results,
        docs_total,
    ) = current_task.next_document_for_user_mqmesa(request.user)

    if not next_item:
        if not ajax:
            LOGGER.info('No next item detected, redirecting to dashboard')
            return redirect('dashboard')
        else:
            context = {}
            ajax_context = {'saved': item_saved, 'error_msg': error_msg}
            context.update(ajax_context)
            context.update(BASE_CONTEXT)
            # Send response to the Ajax POST request
            return JsonResponse(context)

    # TODO: hotfix for WMT24
    # Tracking issue: https://github.com/AppraiseDev/Appraise/issues/185
    for item in doc_items:
        # don't escape HTML video
        if item.sourceText.strip().startswith("<video"):
            continue
        item.sourceText = escape(item.sourceText)

    # Get item scores from the latest corresponding results
    doc_items_results = [
        {
            'completed': bool(result and result.completed),
            # will be recomputed user-side anyway
            'score': result.score if result else -1,
            'mqm': result.mqm if result else item.mqm,
            'mqm_orig': item.mqm,
            'start_timestamp': result.start_time if result else "",
            'end_timestamp': result.end_time if result else "",
        }
        for item, result in zip(doc_items, doc_items_results)
    ]

    LOGGER.info(f'items_completed={items_completed}, docs_completed={docs_completed}')

    source_language = current_task.marketSourceLanguage()
    target_language = current_task.marketTargetLanguage()

    guidelines = ""
    if 'contrastiveesa' in campaign_opts:
        # escape <br/> tags in the source and target texts
        for item in doc_items:
            item.sourceText = item.sourceText \
                .replace("&lt;eos&gt;", "<code>&lt;eos&gt;</code>") \
                .replace("&lt;br/&gt;", "<br/>")
            # HTML-esaping on the target text will not work because MQM/ESA tag insertion prevents it 
        guidelines = (
            'You are provided with a text in {0} and its candidate translation(s) into {1}. '
            'Please assess the quality of the translation(s) following the detailed guidelines below. '.format(
                source_language,
                target_language,
            )
        )

    # A part of context used in responses to both Ajax and standard POST requests
    context = {
        'active_page': 'direct-assessment-document',
        'item_id': next_item.itemID,
        'task_id': next_item.id,
        'document_id': next_item.documentID,
        'items_completed': items_completed,
        'docs_completed': docs_completed,
        'docs_total': docs_total,
        'source_language': source_language,
        'target_language': target_language,
        'campaign': campaign.campaignName,
        # Task variations
        'ui_lang': "enu",
        'mqm_type': 'ESA' if 'esa' in campaign_opts else "MQM",
        'guidelines': guidelines,
    }

    if ajax:
        ajax_context = {'saved': item_saved, 'error_msg': error_msg}
        context.update(ajax_context)
        context.update(BASE_CONTEXT)
        # Send response to the Ajax POST request
        return JsonResponse(context)

    page_context = {
        'items': zip(doc_items, doc_items_results),
        'reference_label': 'Source text',
        'candidate_label': 'Candidate translation',
    }
    context.update(page_context)
    context.update(BASE_CONTEXT)

    return render(request, 'EvalView/direct-assessment-document-mqm-esa.html', context)


# pylint: disable=C0103,C0330
@login_required
def multimodal_assessment(request, code=None, campaign_name=None):
    """
    Multi modal assessment annotation view.
    """
    t1 = datetime.now()

    campaign = None
    if campaign_name:
        campaign = Campaign.objects.filter(campaignName=campaign_name)
        if not campaign.exists():
            _msg = 'No campaign named "%s" exists, redirecting to dashboard'
            LOGGER.info(_msg, campaign_name)
            return redirect('dashboard')

        campaign = campaign[0]

    LOGGER.info(
        'Rendering multimodal assessment view for user "%s".',
        request.user.username or "Anonymous",
    )

    current_task = None

    # Try to identify TaskAgenda for current user.
    agendas = TaskAgenda.objects.filter(user=request.user)

    if campaign:
        agendas = agendas.filter(campaign=campaign)

    for agenda in agendas:
        modified = False
        LOGGER.info('Identified work agenda %s', agenda)

        tasks_to_complete = []
        for serialized_open_task in agenda.serialized_open_tasks():
            open_task = serialized_open_task.get_object_instance()

            # Skip tasks which are not available anymore
            if open_task is None:
                continue

            if open_task.next_item_for_user(request.user) is not None:
                current_task = open_task
                if not campaign:
                    campaign = agenda.campaign
            else:
                tasks_to_complete.append(serialized_open_task)

        for task in tasks_to_complete:
            modified = agenda.complete_open_task(task) or modified

        if modified:
            agenda.save()

    if not current_task and agendas.count() > 0:
        LOGGER.info('Work agendas completed, redirecting to dashboard')
        LOGGER.info('- code=%s, campaign=%s', code, campaign)
        return redirect('dashboard')

    # If language code has been given, find a free task and assign to user.
    if not current_task:
        current_task = MultiModalAssessmentTask.get_task_for_user(user=request.user)

    if not current_task:
        if code is None or campaign is None:
            LOGGER.info('No current task detected, redirecting to dashboard')
            LOGGER.info('- code=%s, campaign=%s', code, campaign)
            return redirect('dashboard')

        _msg = 'Identifying next task for code "%s", campaign="%s"'
        LOGGER.info(_msg, code, campaign)
        next_task = MultiModalAssessmentTask.get_next_free_task_for_language(
            code, campaign, request.user
        )

        if next_task is None:
            LOGGER.info('No next task detected, redirecting to dashboard')
            return redirect('dashboard')

        next_task.assignedTo.add(request.user)
        next_task.save()

        current_task = next_task

    if current_task:
        if not campaign:
            campaign = current_task.campaign

        elif campaign.campaignName != current_task.campaign.campaignName:
            _msg = 'Incompatible campaign given, using item campaign instead!'
            LOGGER.info(_msg)
            campaign = current_task.campaign

    t2 = datetime.now()
    if request.method == "POST":
        score = request.POST.get('score', None)
        item_id = request.POST.get('item_id', None)
        task_id = request.POST.get('task_id', None)
        start_timestamp = request.POST.get('start_timestamp', None)
        end_timestamp = request.POST.get('end_timestamp', None)
        LOGGER.info('score=%s, item_id=%s', score, item_id)
        if score and item_id and start_timestamp and end_timestamp:
            duration = float(end_timestamp) - float(start_timestamp)
            LOGGER.debug(float(start_timestamp))
            LOGGER.debug(float(end_timestamp))
            LOGGER.info(
                'start=%s, end=%s, duration=%s',
                start_timestamp,
                end_timestamp,
                duration,
            )

            current_item = current_task.next_item_for_user(request.user)
            if current_item.itemID != int(item_id) or current_item.id != int(task_id):
                _msg = 'Item ID %s does not match  item %s, will not save!'
                LOGGER.debug(_msg, item_id, current_item.itemID)

            else:
                utc_now = datetime.utcnow().replace(tzinfo=utc)

                # pylint: disable=E1101
                MultiModalAssessmentResult.objects.create(
                    score=score,
                    start_time=float(start_timestamp),
                    end_time=float(end_timestamp),
                    item=current_item,
                    task=current_task,
                    createdBy=request.user,
                    activated=False,
                    completed=True,
                    dateCompleted=utc_now,
                )

    t3 = datetime.now()

    current_item, completed_items = current_task.next_item_for_user(
        request.user, return_completed_items=True
    )
    if not current_item:
        LOGGER.info('No current item detected, redirecting to dashboard')
        return redirect('dashboard')

    # completed_items_check = current_task.completed_items_for_user(
    #     request.user)
    completed_blocks = int(completed_items / 10)
    _msg = 'completed_items=%s, completed_blocks=%s'
    LOGGER.info(_msg, completed_items, completed_blocks)

    source_language = current_task.marketSourceLanguage()
    target_language = current_task.marketTargetLanguage()

    t4 = datetime.now()

    context = {
        'active_page': 'multimodal-assessment',
        'reference_text': current_item.sourceText,
        'candidate_text': current_item.targetText,
        'image_url': current_item.imageURL,
        'item_id': current_item.itemID,
        'task_id': current_item.id,
        'completed_blocks': completed_blocks,
        'items_left_in_block': 10 - (completed_items - completed_blocks * 10),
        'source_language': source_language,
        'target_language': target_language,
        'debug_times': (t2 - t1, t3 - t2, t4 - t3, t4 - t1),
        'template_debug': 'debug' in request.GET,
        'campaign': campaign.campaignName,
        'datask_id': current_task.id,
        'trusted_user': current_task.is_trusted_user(request.user),
    }
    context.update(BASE_CONTEXT)

    return render(request, 'EvalView/multimodal-assessment.html', context)


# pylint: disable=C0103,C0330
@login_required
def pairwise_assessment(request, code=None, campaign_name=None):
    """
    Pairwise direct assessment annotation view.
    """
    # Import at the top of the function to make it available everywhere in the function
    from EvalData.models import PairwiseAssessmentResult
    
    # Redirect to introduction page if coming here directly
    if not request.session.get('visited_introduction', False):
        return redirect('pairwise-introduction') # added this to redirect to the introduction page

    t1 = datetime.now()

    campaign = None
    if campaign_name:
        campaign = Campaign.objects.filter(campaignName=campaign_name)
        if not campaign.exists():
            _msg = 'No campaign named "%s" exists, redirecting to dashboard'
            LOGGER.info(_msg, campaign_name)
            return redirect('dashboard')

        campaign = campaign[0]

    LOGGER.info(
        'Rendering pairwise direct assessment view for user "%s".',
        request.user.username or "Anonymous",
    )

    current_task = None
    edit_mode = request.session.get('edit_mode', False)

    # Try to identify TaskAgenda for current user.
    agendas = TaskAgenda.objects.filter(user=request.user)

    if campaign:
        agendas = agendas.filter(campaign=campaign)

    for agenda in agendas:
        LOGGER.info('Identified work agenda %s', agenda)

        tasks_to_complete = []
        for serialized_open_task in agenda.serialized_open_tasks():
            open_task = serialized_open_task.get_object_instance()

            # Skip tasks which are not available anymore
            if open_task is None:
                continue

            # In edit mode, we want to use a different approach to get the next item
            if edit_mode:
                # Get items with results that need to be edited
                results = PairwiseAssessmentResult.objects.filter(
                    createdBy=request.user,
                    task=open_task,
                    completed=False  # We marked them as incomplete in change_answers
                )
                
                if results.exists():
                    current_task = open_task
                    if not campaign:
                        campaign = agenda.campaign
                    break
            elif open_task.next_item_for_user(request.user) is not None:
                current_task = open_task
                if not campaign:
                    campaign = agenda.campaign
            else:
                tasks_to_complete.append(serialized_open_task)

        # If we found a task in edit mode, break out of the agenda loop
        if edit_mode and current_task:
            break
            
        modified = False
        for task in tasks_to_complete:
            modified = agenda.complete_open_task(task) or modified

        if modified:
            agenda.save()

    if not current_task and agendas.count() > 0:
        LOGGER.info('Work agendas completed, redirecting to dashboard')
        LOGGER.info('- code=%s, campaign=%s', code, campaign)
        
        # If in edit mode, redirect to feedback page instead of dashboard
        if edit_mode:
            messages.success(request, "You have completed editing your answers. Please submit your feedback.")
            return redirect('pairwise-feedback')
        
        return redirect('dashboard')

    # If language code has been given, find a free task and assign to user.
    if not current_task:
        current_task = PairwiseAssessmentTask.get_task_for_user(user=request.user)

    if not current_task:
        if code is None or campaign is None:
            LOGGER.info('No current task detected, redirecting to dashboard')
            LOGGER.info('- code=%s, campaign=%s', code, campaign)
            
            # If in edit mode, redirect to feedback page instead of dashboard
            if edit_mode:
                messages.success(request, "You have completed editing your answers. Please submit your feedback.")
                return redirect('pairwise-feedback')
                
            return redirect('dashboard')

        LOGGER.info(
            'Identifying next task for code "%s", campaign="%s"',
            code,
            campaign,
        )
        next_task = PairwiseAssessmentTask.get_next_free_task_for_language(
            code, campaign, request.user
        )

        if next_task is None:
            LOGGER.info('No next task detected, redirecting to dashboard')
            
            # If in edit mode, redirect to feedback page instead of dashboard
            if edit_mode:
                messages.success(request, "You have completed editing your answers. Please submit your feedback.")
                return redirect('pairwise-feedback')
                
            return redirect('dashboard')

        next_task.assignedTo.add(request.user)
        next_task.save()

        current_task = next_task

    if current_task:
        if not campaign:
            campaign = current_task.campaign

        elif campaign.campaignName != current_task.campaign.campaignName:
            _msg = 'Incompatible campaign given, using item campaign instead!'
            LOGGER.info(_msg)
            campaign = current_task.campaign

    t2 = datetime.now()

    # Use a custom approach to get the next item in edit mode
    current_item = None
    if edit_mode:
        # Get the next incomplete result for this task
        result = PairwiseAssessmentResult.objects.filter(
            createdBy=request.user,
            task=current_task,
            completed=False
        ).first()
        
        if result:
            current_item = result.item
    else:
        current_item = current_task.next_item_for_user(request.user)

    if not current_item:
        LOGGER.info('No current item detected, redirecting to feedback page')
        return redirect('pairwise-feedback')

    candidate1_text, candidate2_text = current_item.target_texts_with_diffs()

    candidate1_diffs = extract_marked_spans(candidate1_text)
    candidate2_diffs = extract_marked_spans(candidate2_text)

    # Check if we're in edit mode and get previous answers
    previous_answers = None
    if request.session.get('edit_mode', False):
        # Get previous answers from session if available
        previous_results = request.session.get('previous_results', {})
        key = f"{current_task.id}_{current_item.id}"
        
        if key in previous_results:
            previous_answers_data = previous_results[key]
            LOGGER.info(f"Found previous answers for item {key} in session")
            LOGGER.info(f"Previous answers data: {previous_answers_data}")
            
            # Add debug info for span differences
            LOGGER.info(f"Span diff votes: {previous_answers_data.get('span_diff_votes', [])}")
            LOGGER.info(f"Span diff explanations: {previous_answers_data.get('span_diff_explanations', [])}")
            LOGGER.info(f"Span diff other texts: {previous_answers_data.get('span_diff_other_texts', [])}")
            
            # Ensure arrays have correct length
            max_diffs = max(len(candidate1_diffs), len(candidate2_diffs)) #len(diff_pairs)
            span_diff_votes = previous_answers_data.get('span_diff_votes', [])
            span_diff_explanations = previous_answers_data.get('span_diff_explanations', [])
            span_diff_other_texts = previous_answers_data.get('span_diff_other_texts', [])
            
            # Extend arrays if needed
            while len(span_diff_votes) < max_diffs:
                span_diff_votes.append('')
            while len(span_diff_explanations) < max_diffs:
                span_diff_explanations.append([])
            while len(span_diff_other_texts) < max_diffs:
                span_diff_other_texts.append('')
                
            # Update the data with corrected arrays
            previous_answers_data['span_diff_votes'] = span_diff_votes
            previous_answers_data['span_diff_explanations'] = span_diff_explanations
            previous_answers_data['span_diff_other_texts'] = span_diff_other_texts
            
            LOGGER.info(f"Updated span diff arrays to match {max_diffs} differences")
        else:
            # If not in session, try to find in database (fallback)
            previous_answers = PairwiseAssessmentResult.objects.filter(
                createdBy=request.user,
                item=current_item,
                task=current_task
            ).order_by('-id').first()
            
            if previous_answers:
                # Process the data with careful handling of potentially empty values
                span_diff_votes = previous_answers.span_diff_votes.split(';\n') if previous_answers.span_diff_votes else []
                
                # Better handling of span_diff_explanations
                span_diff_explanations = []
                if previous_answers.span_diff_explanations:
                    # Split by semicolon-newline to get each diff's explanations
                    for explanation_str in previous_answers.span_diff_explanations.split(';\n'):
                        if explanation_str:
                            # Each explanation string is plus-separated choices
                            # Split by " + " and strip each choice to handle any extra spaces
                            span_diff_explanations.append([choice.strip() for choice in explanation_str.split(' + ')])
                        else:
                            span_diff_explanations.append([])
                
                # Make sure we have equal number of entries for all span diffs
                max_diffs = max(len(candidate1_diffs), len(candidate2_diffs)) #len(diff_pairs)
                
                # Extend span_diff_votes if needed
                while len(span_diff_votes) < max_diffs:
                    span_diff_votes.append('')
                
                # Extend span_diff_explanations if needed
                while len(span_diff_explanations) < max_diffs:
                    span_diff_explanations.append([])
                
                # Process span_diff_other_texts
                span_diff_other_texts = []
                if previous_answers.span_diff_other_texts:
                    span_diff_other_texts = previous_answers.span_diff_other_texts.split(';\n')
                
                # Extend span_diff_other_texts if needed
                while len(span_diff_other_texts) < max_diffs:
                    span_diff_other_texts.append('')
                
                # Debug logging
                LOGGER.info(f"Found {len(span_diff_votes)} span diff votes, {len(span_diff_explanations)} explanations, and {len(span_diff_other_texts)} other texts")
                for i, (votes, exps, others) in enumerate(zip(span_diff_votes, span_diff_explanations, span_diff_other_texts)):
                    LOGGER.info(f"Diff {i}: Vote={votes}, Explanations={exps}, Other={others}")
                
                previous_answers_data = {
                    'score1': previous_answers.score1,
                    'score2': previous_answers.score2,
                    'selected_translation': previous_answers.selected_translation,
                    'selected_advantages': previous_answers.selected_advantages.split(';\n') if previous_answers.selected_advantages else [],
                    'selected_advantages_other': previous_answers.selected_advantages_other or '',
                    'non_selected_problems': previous_answers.non_selected_problems.split(';\n') if previous_answers.non_selected_problems else [],
                    'non_selected_problems_other': previous_answers.non_selected_problems_other or '',
                    'wiki_adequacy': previous_answers.wiki_adequacy.split(';\n') if previous_answers.wiki_adequacy else [],
                    'span_diff_votes': span_diff_votes,
                    'span_diff_explanations': span_diff_explanations,
                    'span_diff_other_texts': span_diff_other_texts
                }
                LOGGER.info(f"Found previous answers for item {key} in database")
            else:
                previous_answers_data = None
                LOGGER.info(f"No previous answers found for item {key}")
    else:
        previous_answers_data = None

    if request.method == "POST":
        score1 = request.POST.get('score', None)  # TODO: score -> score1
        score2 = request.POST.get('score2', None)
        item_id = request.POST.get('item_id', None)
        task_id = request.POST.get('task_id', None)
        start_timestamp = request.POST.get('start_timestamp', None)
        end_timestamp = request.POST.get('end_timestamp', None)

        source_error = request.POST.get('source_error', None)
        error1 = request.POST.get('error1', None)
        error2 = request.POST.get('error2', None)

        # Hiba added this: retrieve freetextannotation from POST data
        Free_Text_Annotation = request.POST.get('FreeTextAnnotation', '').strip()
        # Get the new three fields from POST data
        selected_advantages = request.POST.getlist('selected_advantages', [])
        selected_advantages_other = request.POST.get('selected_advantages_other', '')
        non_selected_problems = request.POST.getlist('non_selected_problems', [])
        non_selected_problems_other = request.POST.get('non_selected_problems_other', '')
        wiki_adequacy = request.POST.getlist('wiki_adequacy', [])
        # Get intro survey data
        # ============================
        # Capture span-level diff votes
        diff_choices = []
        diff_explanations = []
        diff_other_texts = []
        i = 0
        while True:
            choice = request.POST.get(f"diff_vote_{i}")
            if choice is None:
                break
            diff_choices.append(choice)
            
            # Get explanations for this diff
            explanations = request.POST.getlist(f"selected_choices_diff_{i}")
            print(f"DEBUG - Raw explanations for diff {i}:", explanations)  # Debug print
            diff_explanations.append(" + ".join(explanations) if explanations else "")
            
            # Get other text for this diff - fixed field name to match template
            other_text_diff = request.POST.get(f"other_text_diff_{i}", "")
            print(f"DEBUG - Other text for diff {i}:", other_text_diff)  # Add debug print
            diff_other_texts.append(other_text_diff)
            
            i += 1



        print("Collected diff_choices:", diff_choices)
        print("Collected diff_explanations:", diff_explanations)
        print("Collected diff_other_texts:", diff_other_texts)
        # ============================
        # Hiba added this: retrieve selected choices from POST data
        # ============================

        wikipedia_familiarity = request.POST.getlist('wikipedia_familiarity', [])
        other_wikipedia_familiarity_text = request.POST.get('other_wikipedia_familiarity_text', '')
        fluency_in_target_language = request.POST.get('fluency_in_target_language', '')
        # Get post survey data
        feedback_options = request.POST.getlist('feedback_options', [])
        other_feedback_options_text = request.POST.get('other_feedback_options_text', '')
        overallExperience = request.POST.get('overallExperience', '')

    
        # Validate "Other" option
        if 'other' in selected_advantages and not selected_advantages_other:
            messages.error(request, "Please specify details for 'Other'.")
            return redirect(request.path)
        
        # Validate "Other" option
        if 'other' in non_selected_problems and not non_selected_problems_other:
            messages.error(request, "Please specify details for 'Other'.")
            return redirect(request.path)

        

        # Validate "Other" option for each diff
        i = 0
        while True:
            choice = request.POST.get(f"diff_vote_{i}")
            if choice is None:  # No more diffs to process
                break
                
            explanations = request.POST.getlist(f"selected_choices_diff_{i}")
            if 'Other' in explanations:
                other_text = request.POST.get(f"other_text_diff_{i}", "").strip()
                if not other_text:
                    messages.error(request, f"Please specify details for 'Other' in Difference {i + 1}.")
                    return redirect(request.path)
            i += 1

        print(
        'score1={0}, score2={1}, item_id={2}, src_err={3}, error1={4}, error2={5}, freetextannotation={6}'.format(
            score1, score2, item_id, source_error, error1, error2, Free_Text_Annotation
            ))
        LOGGER.info(
        'score1=%s, score2=%s, item_id=%s, freetextannotation=%s', score1, score2, item_id, Free_Text_Annotation)

        '''
        print(
            'score1={0}, score2={1}, item_id={2}, src_err={3}, error1={4}, error2={5}'.format(
                score1, score2, item_id, source_error, error1, error2
            )
        )
        LOGGER.info('score1=%s, score2=%s, item_id=%s', score1, score2, item_id)
        '''
        

        if score1 and item_id and start_timestamp and end_timestamp:
            duration = float(end_timestamp) - float(start_timestamp)
            LOGGER.debug(float(start_timestamp))
            LOGGER.debug(float(end_timestamp))
            LOGGER.info(
                'start=%s, end=%s, duration=%s',
                start_timestamp,
                end_timestamp,
                duration,
            )

            current_item = current_task.next_item_for_user(request.user)
            if current_item.itemID != int(item_id) or current_item.id != int(task_id):
                _msg = 'Item ID %s does not match item %s, will not save!'
                LOGGER.debug(_msg, item_id, current_item.itemID)

            else:
                utc_now = datetime.utcnow().replace(tzinfo=utc)

                # Read intro values from session
                wikipedia_familiarity = request.session.get("wikipedia_familiarity", [])
                other_wikipedia_familiarity_text = request.session.get("other_wikipedia_familiarity_text", "")
                fluency_in_target_language = request.session.get("fluency_in_target_language", "")

                # Optionally log them to confirm:
                print("SAVING SESSION FIELDS:")
                print("wikipedia_familiarity =", wikipedia_familiarity)
                print("other_wikipedia_familiarity_text =", other_wikipedia_familiarity_text)
                print("fluency_in_target_language =", fluency_in_target_language)


                print("SESSION - wikipedia_familiarity:", request.session.get("wikipedia_familiarity"))
                print("SESSION - other_wikipedia_familiarity_text:", request.session.get("other_wikipedia_familiarity_text"))
                print("SESSION - fluency:", request.session.get("fluency_in_target_language"))

                print("POST - feedback_options:", request.POST.getlist("feedback_options"))
                print("POST - overallExperience:", request.POST.get("overallExperience"))



                # 1) grab the raw texts, not the annotated ones:
                raw1 = current_item.target1Text
                raw2 = current_item.target2Text

                # 2) tokenize exactly as target_texts_with_diffs does:
                toks1 = raw1.split()
                toks2 = raw2.split()

                # 3) align them
                matcher = SequenceMatcher(None, toks1, toks2)
                diff_pairs = []
                #span_diff_texts = ""

                for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                    if tag == 'equal':
                        continue

                    if tag == 'replace':
                        a = " ".join(toks1[i1:i2]).strip() or " "
                        b = " ".join(toks2[j1:j2]).strip() or " "
                        diff_pairs.append((a, b))

                    elif tag == 'delete':
                        a = " ".join(toks1[i1:i2]).strip() or " "
                        diff_pairs.append((a, " "))

                    elif tag == 'insert':
                        b = " ".join(toks2[j1:j2]).strip() or " "
                        diff_pairs.append((" ", b))

                span_diff_texts = (";\n".join(f"{old.strip()} |vs| {new.strip()}" for old, new in diff_pairs) if diff_pairs else "")

                print("Collected diff_pairs:", diff_pairs)
                print("Collected span_diff_texts:", span_diff_texts)
                # pylint: disable=E1101
                PairwiseAssessmentResult.objects.create(
                    score1=score1,
                    score2=score2,
                    start_time=float(start_timestamp),
                    end_time=float(end_timestamp),
                    item=current_item,
                    task=current_task,
                    createdBy=request.user,
                    activated=False,
                    completed=True,
                    dateCompleted=utc_now,
                    sourceErrors=source_error,
                    errors1=error1,
                    errors2=error2,
                    selected_advantages=";\n".join(selected_advantages),
                    selected_advantages_other=selected_advantages_other,
                    non_selected_problems=";\n".join(non_selected_problems),
                    non_selected_problems_other=non_selected_problems_other,
                    wiki_adequacy=";\n".join(wiki_adequacy),
                    wikipedia_familiarity=';\n'.join(wikipedia_familiarity),
                    other_wikipedia_familiarity_text=other_wikipedia_familiarity_text,
                    fluency_in_target_language=fluency_in_target_language,
                    feedback_options=";\n".join(feedback_options),
                    other_feedback_options_text=other_feedback_options_text,
                    overallExperience=overallExperience,
                    span_diff_votes=";\n".join(diff_choices),
                    span_diff_explanations=";\n".join(diff_explanations),
                    span_diff_other_texts=";\n".join(diff_other_texts),
                    span_diff_texts=span_diff_texts,
                )

    t3 = datetime.now()

    current_item, completed_items = current_task.next_item_for_user(
        request.user, return_completed_items=True
    )
    if not current_item:
        #LOGGER.info('No current item detected, redirecting to dashboard')
        #return redirect('dashboard')
        LOGGER.info('No current item detected, redirecting to feedback page')
        return redirect('pairwise-feedback')


    # completed_items_check = current_task.completed_items_for_user(
    #     request.user)
    completed_blocks = int(completed_items / 10)
    _msg = 'completed_items=%s, completed_blocks=%s'
    LOGGER.info(_msg, completed_items, completed_blocks)

    source_language = current_task.marketSourceLanguage()
    target_language = current_task.marketTargetLanguage()

    t4 = datetime.now()

    # Define priming question
    #
    # Default:
    #   How accurately does the above candidate text convey the original
    #   semantics of the source text? Slider ranges from
    #   <em>Not at all</em> (left) to <em>Perfectly</em> (right).
    #
    # We currently allow specific overrides, based on campaign name.
    reference_label = 'Source text'
    candidate1_label = 'Candidate translation (1)'
    candidate2_label = 'Candidate translation (2)'

    '''
    priming_question_text = (
        'How accurately does each of the candidate text(s) below convey '
        'the original semantics of the source text above?'
    )
    '''

    # wanted to change the text
    priming_question_text = (
        'Which of the two candidate texts below most accurately and fluently convey '
        'the original meaning of the source text above in the target language? '
        'Simply put: which candidate translation do you prefer?'
    )

    if current_item.has_context():
        # Added 'bolded' to avoid confusion with context sentences that are
        # displayed in a grey color.
        priming_question_text = (
            'How accurately does each of the candidate text(s) below convey '
            'the original semantics of the bolded source text above?'
        )

    (
        candidate1_text,
        candidate2_text,
    ) = current_item.target_texts_with_diffs()

    candidate1_diffs = extract_marked_spans(candidate1_text)
    candidate2_diffs = extract_marked_spans(candidate2_text)

    campaign_opts = set((campaign.campaignOptions or "").lower().split(";"))

    use_sqm = False
    critical_error = False
    source_error = False
    extra_guidelines = False
    doc_guidelines = False
    guidelines_popup = False
    dialect_guidelines = False

    if 'reportcriticalerror' in campaign_opts:
        critical_error = True
        extra_guidelines = True
    if 'reportsourceerror' in campaign_opts:
        source_error = True
        extra_guidelines = True
    if 'sqm' in campaign_opts:
        use_sqm = True
        extra_guidelines = True

    if 'gamingdomainnote' in campaign_opts:
        priming_question_text = (
            'The presented text is a message from an online video game chat. '
            'Please take into account the video gaming genre when making your assessments. </br> '
            + priming_question_text
        )

    if extra_guidelines:
        # note this is not needed if DocLvlGuideline is enabled
        priming_question_text += '<br/> (Please see the detailed guidelines below)'

    if 'doclvlguideline' in campaign_opts:
        use_sqm = True
        doc_guidelines = True
        guidelines_popup = (
            'guidelinepopup' in campaign_opts or 'guidelinespopup' in campaign_opts
        )

    segment_text = current_item.segmentText

    if doc_guidelines:
        priming_question_text = (
            'Above you see a paragraph in {0} and below its corresponding one or two candidate translations in {1}. '
            'Please score the candidate translation(s) below following the detailed guidelines at the bottom of the page '
            '<u><b>paying special attention to document-level properties, '
            'such as consistency of style, selection of translation terms, formality, '
            'and so on</b></u>, in addition to the usual correctness criteria. '.format(
                source_language,
                target_language,
            )
        )

        # process <eos>s and unescape <br/>s
        segment_text = segment_text.replace(
            "&lt;eos&gt;", "<code>&lt;eos&gt;</code>"
        ).replace("&lt;br/&gt;", "<br/>")
        candidate1_text = candidate1_text.replace(
            "&lt;eos&gt;", "<code>&lt;eos&gt;</code>"
        ).replace("&lt;br/&gt;", "<br/>")
        candidate2_text = candidate2_text.replace(
            "&lt;eos&gt;", "<code>&lt;eos&gt;</code>"
        ).replace("&lt;br/&gt;", "<br/>")

    dialect_guidelines = any("dialectsguidelines" in opt for opt in campaign_opts)

    if dialect_guidelines:
        tgt_code = current_task.marketTargetLanguageCode()
        dialect = target_language

        # DialectsGuidelinesA asks for the main dialect for specific languages (fra, ptb, esn)
        if 'dialectsguidelinesa' in campaign_opts:
            if tgt_code == 'fra':
                dialect = "European French (Européen Français)"
            if tgt_code == 'por':
                dialect = "Brazilian Portuguese (Português do Brasil)"
            if tgt_code == 'spa':
                dialect = "European Spanish (Español Europeo)"
        # DialectsGuidelinesB asks for the secondary dialect for specific languages (frc, ptg, esj)
        elif 'dialectsguidelinesb' in campaign_opts:
            if tgt_code == 'fra':
                dialect = "Canadian French (Français Canadien)"
            if tgt_code == 'por':
                dialect = "European Portuguese (Português Europeu)"
            if tgt_code == 'spa':
                dialect = "Latin American Spanish (Español Latinoamericano)"

        # "If there are no significant differences between the candidates, please assign equal scores using the 'Match sliders' button. "
        priming_question_text = (
            "Above you see a segment in {0} and below its corresponding two candidate translations in {1}. "
            "Please evaluate the quality of the candidate translations, "
            "<b class='lang-emph'><u>focusing specifically on the use of the {2} dialect</u></b>. "
            "Pay close attention to dialect-specific language, including vocabulary, idiomatic expressions, and cultural references. "
            "<br/><br/>"
            "In addition to dialect-specific considerations, please also account for common translation errors, "
            "such as accuracy and fluency, as detailed below.".format(
                source_language,
                target_language,
                dialect,
            )
        )

    # Pad shorter candidate diff list
    #max_len = max(len(candidate1_diffs), len(candidate2_diffs))
    #padded_a = candidate1_diffs + [""] * (max_len - len(candidate1_diffs))
    #padded_b = candidate2_diffs + [""] * (max_len - len(candidate2_diffs))

    # Replace empty diffs with placeholder
    #diff_pairs = []
    #for a, b in zip(padded_a, padded_b):
    #    span_a = a.strip() if a.strip() else " "
    #    span_b = b.strip() if b.strip() else " "
    #    diff_pairs.append((span_a, span_b))

    # 1) grab the raw texts, not the annotated ones:
    raw1 = current_item.target1Text
    raw2 = current_item.target2Text

    # 2) tokenize exactly as target_texts_with_diffs does:
    toks1 = raw1.split()
    toks2 = raw2.split()

    # 3) align them
    matcher = SequenceMatcher(None, toks1, toks2)
    diff_pairs = []
    #span_diff_texts = ""

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue

        if tag == 'replace':
            a = " ".join(toks1[i1:i2]).strip() or " "
            b = " ".join(toks2[j1:j2]).strip() or " "
            diff_pairs.append((a, b))

        elif tag == 'delete':
            a = " ".join(toks1[i1:i2]).strip() or " "
            diff_pairs.append((a, " "))

        elif tag == 'insert':
            b = " ".join(toks2[j1:j2]).strip() or " "
            diff_pairs.append((" ", b))

    span_diff_texts = (";\n".join(f"{old.strip()} |vs| {new.strip()}" for old, new in diff_pairs) if diff_pairs else "")

    print("Collected diff_pairs:", diff_pairs)
    print("Collected span_diff_texts:", span_diff_texts)

    context = {
        'active_page': 'pairwise-assessment',
        'reference_label': reference_label,
        'reference_text': segment_text,
        'context_left': current_item.context_left(),
        'context_right': current_item.context_right(),
        'candidate_label': candidate1_label,
        'candidate_text': candidate1_text,
        'candidate2_label': candidate2_label,
        'candidate2_text': candidate2_text,
        'candidate1_diffs': candidate1_diffs,
        'candidate2_diffs': candidate2_diffs,
        #'diff_pairs': list(zip(candidate1_diffs, candidate2_diffs)),
        'diff_pairs': diff_pairs,
        'span_diff_texts' : span_diff_texts,
        'priming_question_text': priming_question_text,
        'item_id': current_item.itemID,
        'task_id': current_item.id,
        'completed_blocks': completed_blocks,
        'items_left_in_block': 10 - (completed_items - completed_blocks * 10),
        'total_segments': 1000,  # or dynamically: total_segments = PairwiseAssessmentTask.objects.filter(...).count()
        'completed_segments': completed_items,
        'segments_left': 1000 - completed_items,
        'source_language': source_language,
        'target_language': target_language,
        'debug_times': (t2 - t1, t3 - t2, t4 - t3, t4 - t1),
        'template_debug': 'debug' in request.GET,
        'campaign': campaign.campaignName,
        'datask_id': current_task.id,
        'trusted_user': current_task.is_trusted_user(request.user),
        'sqm': use_sqm,
        'critical_error': critical_error,
        'source_error': source_error,
        'guidelines_popup': guidelines_popup,
        'doc_guidelines': doc_guidelines,
        'edit_mode': request.session.get('edit_mode', False),
        'previous_answers': previous_answers_data,
    }
    context.update(BASE_CONTEXT)

    #print("===== DEBUGGING SPAN DIFFS!! =====")
    #print("Candidate 1 text:", candidate1_text)
    #print("Candidate 2 text:", candidate2_text)
    #print("Candidate 1 diffs:", candidate1_diffs)
    #print("Candidate 2 diffs:", candidate2_diffs)
    #print("Zipped diff pairs:", list(zip(candidate1_diffs, candidate2_diffs)))
    return render(request, 'EvalView/pairwise-assessment.html', context)


@login_required
def pairwise_introduction(request, code=None, campaign_name=None):
    """
    Displays an introduction page before starting the pairwise assessment.
    Processes Wikipedia contribution data when submitted.
    """
    # Check if we're in edit mode
    edit_mode = request.session.get('edit_mode', False)
    
    if request.method == "POST":
        wikipedia_familiarity = request.POST.getlist('wikipedia_familiarity', [])
        other_wikipedia_familiarity_text = request.POST.get('other_wikipedia_familiarity_text', '')
        fluency_in_target_language = request.POST.get('fluency_in_target_language', '')

        # Save data in session (optional)
        request.session['wikipedia_familiarity'] = wikipedia_familiarity
        request.session['other_wikipedia_familiarity_text'] = other_wikipedia_familiarity_text
        request.session['fluency_in_target_language'] = fluency_in_target_language

        # Mark the intro as visited
        request.session['visited_introduction'] = True

        # Redirect to start annotation (with code and campaign_name if available)
        if code and campaign_name:
            return redirect('pairwise-assessment', code=code, campaign_name=campaign_name)
        return redirect('pairwise-assessment')

    # GET request fallback
    request.session['visited_introduction'] = True
    
    # Pre-populate form fields with previous answers if in edit mode
    previous_data = {
        'wikipedia_familiarity': [],
        'other_wikipedia_familiarity_text': '',
        'fluency_in_target_language': ''
    }
    
    if edit_mode:
        # Try to get previous answers from the most recent result
        from EvalData.models import PairwiseAssessmentResult
        previous_result = PairwiseAssessmentResult.objects.filter(
            createdBy=request.user
        ).order_by('-dateCompleted').first()
        
        if previous_result:
            previous_data = {
                'wikipedia_familiarity': previous_result.wikipedia_familiarity.split(',') if previous_result.wikipedia_familiarity else [],
                'other_wikipedia_familiarity_text': previous_result.other_wikipedia_familiarity_text or '',
                'fluency_in_target_language': previous_result.fluency_in_target_language or ''
            }
            LOGGER.info(f"Pre-populating introduction form with previous answers from result {previous_result.id}")
        else:
            # Fallback to session data if available
            previous_data = {
                'wikipedia_familiarity': request.session.get('wikipedia_familiarity', []),
                'other_wikipedia_familiarity_text': request.session.get('other_wikipedia_familiarity_text', ''),
                'fluency_in_target_language': request.session.get('fluency_in_target_language', '')
            }
            LOGGER.info("Pre-populating introduction form with data from session")
    
    context = {
        'active_page': 'pairwise-introduction',
        'edit_mode': edit_mode,
        'code': code,
        'campaign_name': campaign_name,
        'previous_data': previous_data
    }
    context.update(BASE_CONTEXT)
    return render(request, 'EvalView/pairwise-introduction.html', context)


@login_required
def pairwise_feedback(request):
    """
    Feedback page after completing pairwise assessment.
    """
    # Check if we're in edit mode and show appropriate message
    edit_mode = request.session.get('edit_mode', False)
    
    # Initialize empty previous data
    previous_data = {
        'feedback_options': [],
        'other_feedback_options_text': '',
        'overallExperience': ''
    }
    
    if edit_mode:
        # Try to get previous answers from the most recent result
        from EvalData.models import PairwiseAssessmentResult
        previous_result = PairwiseAssessmentResult.objects.filter(
            createdBy=request.user
        ).order_by('-dateCompleted').first()
        
        if previous_result:
            # Get previous feedback data
            previous_data = {
                'feedback_options': previous_result.feedback_options.split(',') if previous_result.feedback_options else [],
                'other_feedback_options_text': previous_result.other_feedback_options_text or '',
                'overallExperience': previous_result.overallExperience or ''
            }
            LOGGER.info(f"Pre-populating feedback form with previous answers from result {previous_result.id}")
    
    context = {
        'active_page': 'pairwise-feedback',
        'edit_mode': edit_mode,
        'previous_data': previous_data
    }
    context.update(BASE_CONTEXT)
    
    return render(request, 'EvalView/pairwise-feedback.html', context)



@login_required
def pairwise_feedback_submit(request):
    """
    Handle feedback form submission and update assessment results with final feedback.
    """
    if request.method == "POST":
        feedback_text = request.POST.get('feedbackText', '')
        feedback_options = ",".join(request.POST.getlist("feedback_options", []))
        other_feedback_options_text = request.POST.get("other_feedback_options_text", "")
        overall_experience = request.POST.get("overallExperience", "")

        # DEBUG LOGGING
        print("== FEEDBACK SUBMITTED ==")
        print("feedback_options:", feedback_options)
        print("other_feedback_options_text:", other_feedback_options_text)
        print("overallExperience:", overall_experience)

        # Optional: Save feedback_text to a separate model if you want
        # FeedbackModel.objects.create(
        #     user=request.user,
        #     feedback=feedback_text
        # )

        # Update user's most recent assessments with feedback data
        PairwiseAssessmentResult.objects.filter(
            createdBy=request.user
        ).update(
            wikipedia_familiarity=",".join(request.session.get("wikipedia_familiarity", [])),
            other_wikipedia_familiarity_text=request.session.get("other_wikipedia_familiarity_text", ""),
            fluency_in_target_language=request.session.get("fluency_in_target_language", ""),
            feedback_options=feedback_options,
            other_feedback_options_text=other_feedback_options_text,
            overallExperience=overall_experience
        )

        # Clear any edit mode flags and other session data
        if 'edit_mode' in request.session:
            del request.session['edit_mode']
        
        if 'previous_results' in request.session:
            del request.session['previous_results']
        
        # Note: We intentionally don't delete the task agenda here
        # so users can go back and edit their answers from the dashboard

        messages.success(request, "Thank you for your feedback!")
        return redirect('dashboard')

    # If not a POST request, redirect to the feedback form
    return redirect('pairwise-feedback')

# pylint: disable=C0103,C0330
@login_required
def data_assessment(request, code=None, campaign_name=None):
    """
    Direct data assessment annotation view.
    """
    t1 = datetime.now()

    campaign = None
    if campaign_name:
        campaign = Campaign.objects.filter(campaignName=campaign_name)
        if not campaign.exists():
            _msg = 'No campaign named "%s" exists, redirecting to dashboard'
            LOGGER.info(_msg, campaign_name)
            return redirect('dashboard')

        campaign = campaign[0]

    LOGGER.info(
        'Rendering direct assessment view for user "%s".',
        request.user.username or "Anonymous",
    )

    current_task = None

    # Try to identify TaskAgenda for current user.
    agendas = TaskAgenda.objects.filter(user=request.user)

    if campaign:
        agendas = agendas.filter(campaign=campaign)

    for agenda in agendas:
        LOGGER.info('Identified work agenda %s', agenda)

        tasks_to_complete = []
        for serialized_open_task in agenda.serialized_open_tasks():
            open_task = serialized_open_task.get_object_instance()

            # Skip tasks which are not available anymore
            if open_task is None:
                continue

            if open_task.next_item_for_user(request.user) is not None:
                current_task = open_task
                if not campaign:
                    campaign = agenda.campaign
            else:
                tasks_to_complete.append(serialized_open_task)

        modified = False
        for task in tasks_to_complete:
            modified = agenda.complete_open_task(task) or modified

        if modified:
            agenda.save()

    if not current_task and agendas.count() > 0:
        LOGGER.info('Work agendas completed, redirecting to dashboard')
        LOGGER.info('- code=%s, campaign=%s', code, campaign)
        return redirect('dashboard')

    # If language code has been given, find a free task and assign to user.
    if not current_task:
        current_task = DataAssessmentTask.get_task_for_user(user=request.user)

    if not current_task:
        if code is None or campaign is None:
            LOGGER.info('No current task detected, redirecting to dashboard')
            LOGGER.info('- code=%s, campaign=%s', code, campaign)
            return redirect('dashboard')

        LOGGER.info(
            'Identifying next task for code "%s", campaign="%s"',
            code,
            campaign,
        )
        next_task = DataAssessmentTask.get_next_free_task_for_language(
            code, campaign, request.user
        )

        if next_task is None:
            LOGGER.info('No next task detected, redirecting to dashboard')
            return redirect('dashboard')

        next_task.assignedTo.add(request.user)
        next_task.save()

        current_task = next_task

    if current_task:
        if not campaign:
            campaign = current_task.campaign

        elif campaign.campaignName != current_task.campaign.campaignName:
            _msg = 'Incompatible campaign given, using item campaign instead!'
            LOGGER.info(_msg)
            campaign = current_task.campaign

    t2 = datetime.now()
    if request.method == "POST":
        score = request.POST.get('score', None)
        rank = request.POST.get('rank', None)
        item_id = request.POST.get('item_id', None)
        task_id = request.POST.get('task_id', None)
        start_timestamp = request.POST.get('start_timestamp', None)
        end_timestamp = request.POST.get('end_timestamp', None)

        _msg = 'score={} rank={} item_id={}'.format(score, rank, item_id)
        LOGGER.info(_msg)
        print(_msg)

        if score is None:
            print('No score provided, will not save!')
        elif item_id and start_timestamp and end_timestamp:
            duration = float(end_timestamp) - float(start_timestamp)
            LOGGER.debug(float(start_timestamp))
            LOGGER.debug(float(end_timestamp))
            LOGGER.info(
                'start=%s, end=%s, duration=%s',
                start_timestamp,
                end_timestamp,
                duration,
            )

            current_item = current_task.next_item_for_user(request.user)
            if current_item.itemID != int(item_id) or current_item.id != int(task_id):
                _msg = 'Item ID %s does not match item %s, will not save!'
                LOGGER.debug(_msg, item_id, current_item.itemID)

            else:
                utc_now = datetime.utcnow().replace(tzinfo=utc)

                # pylint: disable=E1101
                DataAssessmentResult.objects.create(
                    score=score,
                    rank=rank,
                    start_time=float(start_timestamp),
                    end_time=float(end_timestamp),
                    item=current_item,
                    task=current_task,
                    createdBy=request.user,
                    activated=False,
                    completed=True,
                    dateCompleted=utc_now,
                )

    t3 = datetime.now()

    current_item, completed_items = current_task.next_item_for_user(
        request.user, return_completed_items=True
    )
    if not current_item:
        LOGGER.info('No current item detected, redirecting to dashboard')
        return redirect('dashboard')

    completed_blocks = int(completed_items / 10)
    _msg = 'completed_items=%s, completed_blocks=%s'
    LOGGER.info(_msg, completed_items, completed_blocks)

    source_language = current_task.marketSourceLanguage()
    target_language = current_task.marketTargetLanguage()

    t4 = datetime.now()

    source_label = 'Source text'
    target_label = 'Translation'
    top_question_text = [
        'You are presented a fragment of a document in {src} and {trg}. '.format(
            src=source_language, trg=target_language
        ),
        'Please judge the quality of the translations between the documents on a scale from poor (left) to perfect (right), '
        'taking in to account aspects like adequacy, fluency, writing ability, orthography, style, misalignments, etc. ',
        'Please consider these aspects in both the {src} and {trg} part. '
        'For example, poor fluency in the {src} fragment is a problem too. '
        'While you may use the context from the other sentences in the document, '
        'the translations need to be correct at the sentence level.'.format(
            src=source_language, trg=target_language
        ),
    ]
    score_question_text = [
        'Question #1: '
        'What is the quality of the translations, taking in to account aspects like '
        'adequacy, fluency, writing ability, orthography, style, misalignments, etc.?'
    ]
    rank_question_text = [
        'Question #2: '
        'Do you think any of the sentences ({src} or {trg}) '
        'were created by machine translation, rather than written by a human?'.format(
            src=source_language, trg=target_language
        ),
    ]

    # There should be exactly 4 ranks, otherwise change 'col-sm-3' in the HTML view.
    # Each tuple includes radio label and radio value.
    ranks = [
        ('Definitely machine-translated', 1),
        ('Possibly machine-translated', 2),
        ('Possibly human-written', 3),
        ('Definitely human-written', 4),
    ]

    parallel_data = list(current_item.get_sentence_pairs())

    campaign_opts = set((campaign.campaignOptions or "").lower().split(";"))
    use_sqm = 'sqm' in campaign_opts

    if any(opt in campaign_opts for opt in ['disablemtlabel', 'disablemtrank']):
        ranks = None
        rank_question_text = None
        # remove 'Question #1: '
        score_question_text[0] = score_question_text[0][13:]

    context = {
        'active_page': 'data-assessment',
        'source_label': source_label,
        'target_label': target_label,
        'parallel_data': parallel_data,
        'top_question_text': top_question_text,
        'score_question_text': score_question_text,
        'rank_question_text': rank_question_text,
        'ranks': ranks,
        'sqm': use_sqm,
        'item_id': current_item.itemID,
        'task_id': current_item.id,
        'document_domain': current_item.documentDomain,
        'source_url': current_item.sourceURL,
        'target_url': current_item.targetURL,
        'completed_blocks': completed_blocks,
        'items_left_in_block': 10 - (completed_items - completed_blocks * 10),
        'source_language': source_language,
        'target_language': target_language,
        'debug_times': (t2 - t1, t3 - t2, t4 - t3, t4 - t1),
        'show_debug': 'debug' in request.GET,
        'campaign': campaign.campaignName,
        'datask_id': current_task.id,
        'trusted_user': current_task.is_trusted_user(request.user),
    }
    context.update(BASE_CONTEXT)

    return render(request, 'EvalView/data-assessment.html', context)


# pylint: disable=C0103,C0330
@login_required
def pairwise_assessment_document(request, code=None, campaign_name=None):
    """
    Pairwise direct assessment document annotation view.
    """
    t1 = datetime.now()

    campaign = None
    if campaign_name:
        campaign = Campaign.objects.filter(campaignName=campaign_name)
        if not campaign.exists():
            _msg = 'No campaign named "%s" exists, redirecting to dashboard'
            LOGGER.info(_msg, campaign_name)
            return redirect('dashboard')

        campaign = campaign[0]

    LOGGER.info(
        'Rendering direct assessment document view for user "%s".',
        request.user.username or "Anonymous",
    )

    current_task = None

    # Try to identify TaskAgenda for current user.
    agendas = TaskAgenda.objects.filter(user=request.user)

    if campaign:
        agendas = agendas.filter(campaign=campaign)

    for agenda in agendas:
        LOGGER.info('Identified work agenda %s', agenda)

        tasks_to_complete = []
        for serialized_open_task in agenda.serialized_open_tasks():
            open_task = serialized_open_task.get_object_instance()

            # Skip tasks which are not available anymore
            if open_task is None:
                continue

            if open_task.next_item_for_user(request.user) is not None:
                current_task = open_task
                if not campaign:
                    campaign = agenda.campaign
            else:
                tasks_to_complete.append(serialized_open_task)

        modified = False
        for task in tasks_to_complete:
            modified = agenda.complete_open_task(task) or modified

        if modified:
            agenda.save()

    if not current_task and agendas.count() > 0:
        LOGGER.info('Work agendas completed, redirecting to dashboard')
        LOGGER.info('- code=%s, campaign=%s', code, campaign)
        return redirect('dashboard')

    # If language code has been given, find a free task and assign to user.
    if not current_task:
        current_task = PairwiseAssessmentDocumentTask.get_task_for_user(
            user=request.user
        )

    if not current_task:
        if code is None or campaign is None:
            LOGGER.info('No current task detected, redirecting to dashboard')
            LOGGER.info('- code=%s, campaign=%s', code, campaign)
            return redirect('dashboard')

        LOGGER.info(
            'Identifying next task for code "%s", campaign="%s"',
            code,
            campaign,
        )
        next_task = PairwiseAssessmentDocumentTask.get_next_free_task_for_language(
            code, campaign, request.user
        )

        if next_task is None:
            LOGGER.info('No next task detected, redirecting to dashboard')
            return redirect('dashboard')

        next_task.assignedTo.add(request.user)
        next_task.save()

        current_task = next_task

    if current_task:
        if not campaign:
            campaign = current_task.campaign

        elif campaign.campaignName != current_task.campaign.campaignName:
            _msg = 'Incompatible campaign given, using item campaign instead!'
            LOGGER.info(_msg)
            campaign = current_task.campaign

    # Handling POST requests differs from the original direct_assessment/
    # direct_assessment_context view
    t2 = datetime.now()
    ajax = False
    item_saved = False
    error_msg = ''
    if request.method == "POST":
        score1 = request.POST.get('score1', None)
        score2 = request.POST.get('score2', None)
        item_id = request.POST.get('item_id', None)
        task_id = request.POST.get('task_id', None)
        document_id = request.POST.get('document_id', None)
        start_timestamp = request.POST.get('start_timestamp', None)
        end_timestamp = request.POST.get('end_timestamp', None)
        ajax = bool(request.POST.get('ajax', None) == 'True')

        LOGGER.info('score1=%s, score2=%s, item_id=%s', score1, score2, item_id)
        print(
            'Got request score1={0}, score2={1}, item_id={2}, ajax={3}'.format(
                score1, score2, item_id, ajax
            )
        )

        # If all required information was provided in the POST request
        if score1 and item_id and start_timestamp and end_timestamp:
            duration = float(end_timestamp) - float(start_timestamp)
            LOGGER.debug(float(start_timestamp))
            LOGGER.debug(float(end_timestamp))
            LOGGER.info(
                'start=%s, end=%s, duration=%s',
                start_timestamp,
                end_timestamp,
                duration,
            )

            # Get all items from the document that the submitted item belongs
            # to, and all already collected scores for this document
            (
                current_item,
                block_items,
                block_results,
            ) = current_task.next_document_for_user(
                request.user, return_statistics=False
            )

            # An item from the right document was submitted
            if current_item.documentID == document_id:
                # This is the item that we expected to be annotated first,
                # which means that there is no score for the current item, so
                # create new score
                if current_item.itemID == int(item_id) and current_item.id == int(
                    task_id
                ):

                    utc_now = datetime.utcnow().replace(tzinfo=utc)
                    # pylint: disable=E1101
                    PairwiseAssessmentDocumentResult.objects.create(
                        score1=score1,
                        score2=score2,
                        start_time=float(start_timestamp),
                        end_time=float(end_timestamp),
                        item=current_item,
                        task=current_task,
                        createdBy=request.user,
                        activated=False,
                        completed=True,
                        dateCompleted=utc_now,
                    )
                    print('Item {} (itemID={}) saved'.format(task_id, item_id))
                    item_saved = True

                # It is not the current item, so check if the result for it
                # exists
                else:
                    # Check if there is a score result for the submitted item
                    # TODO: this could be a single query, would it be better or
                    # more effective?
                    current_result = None
                    for result in block_results:
                        if not result:
                            continue
                        if result.item.itemID == int(item_id) and result.item.id == int(
                            task_id
                        ):
                            current_result = result
                            break

                    # If already scored, update the result
                    # TODO: consider adding new score, not updating the
                    # previous one
                    if current_result:
                        prev_score1 = current_result.score1
                        prev_score2 = current_result.score2
                        current_result.score1 = score1
                        current_result.score2 = score2
                        current_result.start_time = float(start_timestamp)
                        current_result.end_time = float(end_timestamp)
                        utc_now = datetime.utcnow().replace(tzinfo=utc)
                        current_result.dateCompleted = utc_now
                        current_result.save()
                        _msg = 'Item {} (itemID={}) updated {}->{} and {}->{}'.format(
                            task_id, item_id, prev_score1, score1, prev_score2, score2
                        )
                        LOGGER.debug(_msg)
                        print(_msg)
                        item_saved = True

                    # If not yet scored, check if the submitted item is from
                    # the expected document. Note that document ID is **not**
                    # sufficient, because there can be multiple documents with
                    # the same ID in the task.
                    else:
                        found_item = False
                        for item in block_items:
                            if item.itemID == int(item_id) and item.id == int(task_id):
                                found_item = item
                                break

                        # The submitted item is from the same document as the
                        # first unannotated item. It is fine, so save it
                        if found_item:
                            utc_now = datetime.utcnow().replace(tzinfo=utc)
                            # pylint: disable=E1101
                            PairwiseAssessmentDocumentResult.objects.create(
                                score1=score1,
                                score2=score2,
                                start_time=float(start_timestamp),
                                end_time=float(end_timestamp),
                                item=found_item,
                                task=current_task,
                                createdBy=request.user,
                                activated=False,
                                completed=True,
                                dateCompleted=utc_now,
                            )
                            _msg = 'Item {} (itemID={}) saved, although it was not the next item'.format(
                                task_id, item_id
                            )
                            LOGGER.debug(_msg)
                            print(_msg)
                            item_saved = True

                        else:
                            error_msg = (
                                'We did not expect this item to be submitted. '
                                'If you used backward/forward buttons in your browser, '
                                'please reload the page and try again.'
                            )

                            _msg = 'Item ID {} does not match item {}, will not save!'.format(
                                item_id, current_item.itemID
                            )
                            LOGGER.debug(_msg)
                            print(_msg)

            # An item from a wrong document was submitted
            else:
                print(
                    'Different document IDs: {} != {}, will not save!'.format(
                        current_item.documentID, document_id
                    )
                )

                error_msg = (
                    'We did not expect an item from this document to be submitted. '
                    'If you used backward/forward buttons in your browser, '
                    'please reload the page and try again.'
                )

    t3 = datetime.now()

    # Get all items from the document that the first unannotated item in the
    # task belongs to, and collect some additional statistics
    (
        current_item,
        completed_items,
        completed_blocks,
        completed_items_in_block,
        block_items,
        block_results,
        total_blocks,
    ) = current_task.next_document_for_user(request.user)

    if not current_item:
        LOGGER.info('No current item detected, redirecting to dashboard')
        return redirect('dashboard')

    campaign_opts = set((campaign.campaignOptions or "").lower().split(";"))
    new_ui = 'newui' in campaign_opts
    escape_eos = 'escapeeos' in campaign_opts
    escape_br = 'escapebr' in campaign_opts
    highlight_style ='highlightstyle' in campaign_opts

    # Get item scores from the latest corresponding results
    block_scores = []
    for item, result in zip(block_items, block_results):
        # Get target texts with injected HTML tags showing diffs
        _candidate1_text, _candidate2_text = item.target_texts_with_diffs(
            escape_html=not new_ui
        )
        if not new_ui:
            _source_text = escape(item.segmentText)
            _default_score = -1
        else:
            _source_text = item.segmentText
            _default_score = 50

        if escape_eos:
            _source_text = _source_text.replace(
                "&lt;eos&gt;", "<code>&lt;eos&gt;</code>"
            )
            _candidate1_text = _candidate1_text.replace(
                "&lt;eos&gt;", "<code>&lt;eos&gt;</code>"
            )
            _candidate2_text = _candidate2_text.replace(
                "&lt;eos&gt;", "<code>&lt;eos&gt;</code>"
            )

        if escape_br:
            _source_text = _source_text.replace("&lt;br/&gt;", "<br/>")
            _candidate1_text = _candidate1_text.replace(
                "&lt;br/&gt;", "<br/>"
            )
            _candidate2_text = _candidate2_text.replace(
                "&lt;br/&gt;", "<br/>"
            )

        item_scores = {
            'completed': bool(result and result.score1 > -1),
            'current_item': bool(item.id == current_item.id),
            'score1': result.score1 if result else _default_score,
            'score2': result.score2 if result else _default_score,
            'candidate1_text': _candidate1_text,
            'candidate2_text': _candidate2_text,
            'segment_text': _source_text,
        }
        block_scores.append(item_scores)

    # completed_items_check = current_task.completed_items_for_user(
    #     request.user)
    _msg = 'completed_items=%s, completed_blocks=%s'
    LOGGER.info(_msg, completed_items, completed_blocks)

    source_language = current_task.marketSourceLanguage()
    target_language = current_task.marketTargetLanguage()

    t4 = datetime.now()

    reference_label = 'Source text'
    candidate1_label = 'Translation A'
    candidate2_label = 'Translation B'

    priming_question_texts = [
        'Below you see a document with {0} sentences in {1} (left columns) '
        'and their corresponding candidate translations from two different systems '
        'in {2} (right columns). '
        'Score each candidate sentence translation in the system\'s document context. '
        'You may revisit already scored sentences and update their scores at any time '
        'by clicking at a source text.'.format(
            len(block_items), source_language, target_language
        ),
        'Assess the translation quality answering the question: ',
        'How accurately does the candidate text for each system (right column, in bold) '
        'convey the original semantics of the source text (left column) in the '
        'system\'s document context? ',
    ]
    document_question_texts = [
        'Please score the overall document translation quality for each system '
        '(you can score the whole documents only after scoring all individual '
        'sentences first).',
        'Assess the translation quality answering the question: ',
        'How accurately does the <strong>entire</strong> candidate document translation '
        'in {0} (right column) convey the original semantics of the source document '
        'in {1} (left column)? '.format(target_language, source_language),
    ]

    monolingual_task = 'monolingual' in campaign_opts
    use_sqm = 'sqm' in campaign_opts
    static_context = 'staticcontext' in campaign_opts
    doc_guidelines = 'doclvlguideline' in campaign_opts
    guidelines_popup = (
        'guidelinepopup' in campaign_opts or 'guidelinespopup' in campaign_opts
    )
    gaming_domain = 'gamingdomainnote' in campaign_opts

    if use_sqm:
        priming_question_texts = priming_question_texts[:1]
        document_question_texts = document_question_texts[:1]

    if monolingual_task:
        source_language = None
        priming_question_texts = [
            'Below you see two documents, each with {0} sentences in {1}. '
            'Score each sentence in both documents in their respective document context. '
            'You may revisit already scored sentences and update their scores at any time '
            'by clicking at a source text.'.format(
                len(block_items) - 1, target_language
            ),
        ]
        document_question_texts = [
            'Please score the overall quality of each document (you can score '
            'the whole document only after scoring all individual sentences from all '
            'documents first).',
        ]
        candidate1_label = 'Translation A'
        candidate2_label = 'Translation B'

    if doc_guidelines:
        priming_question_texts = [
            'Below you see a document with {0} partial paragraphs in {1} (left columns) '
            'and their corresponding two candidate translations in {2} (middle and right column). '
            'Please score each paragraph of both candidate translations '
            '<u><b>paying special attention to document-level properties, '
            'such as consistency of formality and style, selection of translation terms, pronoun choice, '
            'and so on</b></u>, in addition to the usual correctness criteria. '.format(
                len(block_items) - 1,
                source_language,
                target_language,
            ),
        ]

    if gaming_domain:
        priming_question_texts += [
            'The presented texts are messages from an online video game chat. '
            'Please take into account the video gaming genre when making your assessments. </br> '
        ]

    # A part of context used in responses to both Ajax and standard POST
    # requests
    context = {
        'active_page': 'pairwise-assessment-document',
        'item_id': current_item.itemID,
        'task_id': current_item.id,
        'document_id': current_item.documentID,
        'completed_blocks': completed_blocks,
        'total_blocks': total_blocks,
        'items_left_in_block': len(block_items) - completed_items_in_block,
        'source_language': source_language,
        'target_language': target_language,
        'debug_times': (t2 - t1, t3 - t2, t4 - t3, t4 - t1),
        'template_debug': 'debug' in request.GET,
        'campaign': campaign.campaignName,
        'datask_id': current_task.id,
        'trusted_user': current_task.is_trusted_user(request.user),
        'monolingual': monolingual_task,
        'sqm': use_sqm,
        'static_context': static_context,
        'guidelines_popup': guidelines_popup,
        'doc_guidelines': doc_guidelines,
        'highlight_style': highlight_style,
    }

    if ajax:
        ajax_context = {'saved': item_saved, 'error_msg': error_msg}
        context.update(ajax_context)
        context.update(BASE_CONTEXT)
        return JsonResponse(context)  # Sent response to the Ajax POST request

    page_context = {
        'items': zip(block_items, block_scores),
        'num_items': len(block_items),
        'reference_label': reference_label,
        'candidate1_label': candidate1_label,
        'candidate2_label': candidate2_label,
        'priming_question_texts': priming_question_texts,
        'document_question_texts': document_question_texts,
    }
    context.update(page_context)
    context.update(BASE_CONTEXT)

    template = 'EvalView/pairwise-assessment-document.html'
    if new_ui:
        template = 'EvalView/pairwise-assessment-document-newui.html'
    return render(request, template, context)
