from django.shortcuts import render
from .polygon_api import PolygonAPI
from .models import Problem, SampleTestCase, ProblemTestCase, ProblemTag
from django.utils.text import slugify
from django.db.models import Q
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache
import re
import lxml.html
import logging
import json
from django.contrib.auth.decorators import user_passes_test
from django.db import transaction

logger = logging.getLogger(__name__)


def parse_problem_html(html_content):
    """
    Parses Polygon problem.html content and extracts key fields.
    """
    logger.info(
        'Entered parse_problem_html with content length=%d',
        len(html_content)
    )

    tree = lxml.html.fromstring(html_content)

    def get_div_inner_html(
        tree,
        class_name,
        skip_section_title=False
    ):
        div = tree.xpath(f'//div[@class="{class_name}"]')

        if not div:
            return ''

        div = div[0]
        children = div.getchildren()
        html_parts = []

        for child in children:
            if (
                skip_section_title
                and child.tag == 'div'
                and 'section-title' in child.get('class', '')
            ):
                continue

            html_parts.append(
                lxml.html.tostring(
                    child,
                    encoding='unicode'
                )
            )

        content = ''.join(html_parts).strip()

        content = re.sub(
            r'^(<p>\s*</p>)+',
            '',
            content
        )

        return content

    legend = get_div_inner_html(
        tree,
        'legend'
    )

    input_format = get_div_inner_html(
        tree,
        'input-specification',
        skip_section_title=True
    )

    output_format = get_div_inner_html(
        tree,
        'output-specification',
        skip_section_title=True
    )

    title = ''

    title_div = tree.xpath(
        '//div[@class="title"]'
    )

    if title_div:
        title = title_div[0].text_content().strip()

    notes = get_div_inner_html(
        tree,
        'note',
        skip_section_title=True
    )

    logger.debug(
        'Extracted title=%s, legend length=%d, '
        'input_format length=%d, output_format length=%d',
        title,
        len(legend),
        len(input_format),
        len(output_format)
    )

    logger.info(
        'Exiting parse_problem_html'
    )

    return {
        'title': title,
        'legend': legend,
        'input_format': input_format,
        'output_format': output_format,
        'notes': notes,
    }


@user_passes_test(
    lambda u: u.is_authenticated and u.is_staff,
    login_url='/users/login/'
)
def index(request):
    """
    Main view for the problem migration interface.

    Handles:
        - Fetching Polygon problems
        - Migrating problems to database
        - Migrating test cases to database
        - Migrating test cases to configured cloud storage
    """

    logger.info(
        'Entered index view with method=%s',
        request.method
    )

    context = {}

    # Get all existing tags
    all_tags = ProblemTag.objects.all().order_by(
        'tag_name'
    )

    context['all_tags'] = all_tags

    # Serialize tags for JavaScript
    context['all_tags_json'] = json.dumps([
        {
            'pk': tag.id,
            'fields': {
                'name': tag.tag_name
            }
        }
        for tag in all_tags
    ])

    # Initialize selected tags
    if 'selected_tags' not in context:
        context['selected_tags'] = []

    context['selected_tags_json'] = json.dumps(
        context['selected_tags']
    )

    if request.method == 'POST':

        polygon_id = request.POST.get(
            'problem_id'
        )

        # Existing HTML form field name is kept for compatibility
        migrate_to_storage = request.POST.get(
            'migrate_to_azure'
        )

        migrate_to_db = request.POST.get(
            'migrate_to_db'
        )

        migrate_test_cases_to_db = request.POST.get(
            'migrate_test_cases_to_db'
        )

        difficulty = request.POST.get(
            'difficulty',
            ''
        )

        selected_tags = request.POST.getlist(
            'tags'
        )

        new_tag = request.POST.get(
            'new_tag',
            ''
        ).strip()

        logger.debug(
            'POST data: polygon_id=%s, '
            'migrate_to_storage=%s, '
            'migrate_to_db=%s, '
            'migrate_test_cases_to_db=%s, '
            'difficulty=%s, '
            'selected_tags=%s, '
            'new_tag=%s',
            polygon_id,
            migrate_to_storage,
            migrate_to_db,
            migrate_test_cases_to_db,
            difficulty,
            selected_tags,
            new_tag
        )

        logger.debug(
            'All POST data: %s',
            dict(request.POST)
        )

        context['difficulty'] = difficulty
        context['selected_tags'] = selected_tags
        context['new_tag'] = new_tag

        if polygon_id:

            logger.info(
                'Processing polygon_id=%s',
                polygon_id
            )

            api = PolygonAPI()

            context['problem_id'] = polygon_id

            # Check whether problem already exists in database
            db_problem = Problem.objects.filter(
                polygon_id=polygon_id
            ).first()

            logger.debug(
                'db_problem=%s',
                db_problem
            )

            if db_problem:

                context['db_problem'] = db_problem

                # Show currently attached tags
                context['selected_tags'] = [
                    tag.tag_name
                    for tag in db_problem.extra_tags.all()
                ]

                # Fetch stored difficulty
                context['difficulty'] = (
                    db_problem.difficulty
                )

                # Fetch main solution
                if 'main_solution' not in context:

                    main_solution = None

                    try:

                        update_working_copy = api._make_request(
                            'problem.updateWorkingCopy',
                            {
                                'problemId': polygon_id
                            }
                        )

                        logger.debug(
                            'Updated working copy: %s',
                            update_working_copy
                        )

                        solutions = api._make_request(
                            'problem.solutions',
                            {
                                'problemId': polygon_id
                            }
                        )

                        logger.debug(
                            'Fetched solutions: %s',
                            solutions
                        )

                        if solutions:

                            main_solution_name = None

                            for solution in solutions:

                                if solution.get('tag') == 'MA':

                                    main_solution_name = (
                                        solution['name']
                                    )

                                    break

                            if (
                                not main_solution_name
                                and solutions
                            ):
                                main_solution_name = (
                                    solutions[0]['name']
                                )

                            if main_solution_name:

                                main_solution = (
                                    api._make_plain_request(
                                        'problem.viewSolution',
                                        {
                                            'problemId': polygon_id,
                                            'name': main_solution_name
                                        }
                                    )
                                )

                    except Exception as e:

                        logger.error(
                            'Error fetching solution: %s',
                            e
                        )

                        main_solution = None

                    context['main_solution'] = (
                        main_solution
                    )

        try:

            with transaction.atomic():

                # =====================================================
                # HANDLE CLOUD STORAGE MIGRATION
                # =====================================================

                storage_uploaded = False
                storage_problem_id = None

                if migrate_to_storage:

                    logger.info(
                        'Storage migration triggered for polygon_id=%s',
                        polygon_id
                    )

                    problem_obj = Problem.objects.filter(
                        polygon_id=polygon_id
                    ).first()

                    if not problem_obj:

                        logger.warning(
                            'Problem with Polygon ID %s not in DB',
                            polygon_id
                        )

                        context['error'] = (
                            f"Problem with Polygon ID {polygon_id} "
                            "has not been migrated to the database yet. "
                            "Please migrate the problem to the database "
                            "first."
                        )

                        return render(
                            request,
                            'problems/index.html',
                            context
                        )

                    # Database problem ID is used as storage folder
                    problem_id = problem_obj.id

                    logger.info(
                        'Storage migration params: problem_id=%s',
                        problem_id
                    )

                    # Remove cached test cases
                    api.delete_problem_test_case_cache(
                        problem_id
                    )

                    logger.info(
                        'Calling migrate_to_storage'
                    )

                    try:

                        uploaded_count = (
                            api.migrate_to_storage(
                                polygon_id,
                                db_problem_id=problem_id
                            )
                        )

                        storage_uploaded = True
                        storage_problem_id = problem_id

                    except Exception as e:

                        logger.error(
                            'Storage migration failed: %s',
                            e,
                            exc_info=True
                        )

                        context['error'] = (
                            f"Storage migration failed: {str(e)}"
                        )

                        raise

                    logger.info(
                        'Storage migration completed successfully'
                    )

                    provider = (
                        settings.STORAGE_PROVIDER.upper()
                    )

                    success_message = (
                        f"Test cases migrated to "
                        f"{provider} storage successfully. "
                        f"Uploaded {uploaded_count} test cases."
                    )

                    context['success'] = (
                        success_message
                    )

                # =====================================================
                # ALWAYS FETCH PROBLEM DATA FOR DISPLAY
                # =====================================================

                info = api.get_problem_info(
                    polygon_id
                )

                logger.debug(
                    'Polygon problem info: %s',
                    info
                )

                # Download and extract problem package
                problem_html_content = (
                    api.download_and_extract_package(
                        polygon_id
                    )
                )

                logger.debug(
                    'Downloaded and extracted problem_html_content, '
                    'length: %d',
                    len(problem_html_content)
                )

                html_data = parse_problem_html(
                    problem_html_content
                )

                logger.debug(
                    'Parsed html_data: %s',
                    html_data
                )

                # =====================================================
                # FETCH ALL TEST CASES
                # =====================================================

                all_test_cases = []

                if (
                    migrate_to_db
                    or migrate_test_cases_to_db
                    or migrate_to_storage
                ):

                    all_test_cases = (
                        api.get_test_cases_from_redis(
                            polygon_id
                        )
                    )

                    if all_test_cases is None:

                        logger.warning(
                            'Test cases not found in Redis, '
                            'fetching from Polygon'
                        )

                        all_test_cases = (
                            api.get_all_test_cases(
                                polygon_id
                            )
                        )

                        api.store_test_cases_in_redis(
                            polygon_id,
                            all_test_cases,
                            expiry_hours=0.5
                        )

                    else:

                        logger.info(
                            'Retrieved test cases from Redis '
                            'for migration'
                        )

                else:

                    all_test_cases = (
                        api.get_all_test_cases(
                            polygon_id
                        )
                    )

                    logger.debug(
                        'Fetched all test cases.'
                    )

                    api.store_test_cases_in_redis(
                        polygon_id,
                        all_test_cases,
                        expiry_hours=0.5
                    )

                # =====================================================
                # PREPARE TEST CASES FOR DISPLAY
                # =====================================================

                display_test_cases = []

                for test_case in all_test_cases:

                    input_data = test_case.get(
                        'input',
                        ''
                    )

                    output_data = test_case.get(
                        'output',
                        ''
                    )

                    description = test_case.get(
                        'description',
                        ''
                    )

                    display_test_cases.append(
                        {
                            'index': test_case.get(
                                'index',
                                ''
                            ),

                            'input_preview': (
                                input_data[:50]
                                + (
                                    '...'
                                    if len(input_data) > 50
                                    else ''
                                )
                            ),

                            'output_preview': (
                                output_data[:50]
                                + (
                                    '...'
                                    if len(output_data) > 50
                                    else ''
                                )
                            ),

                            'description_preview': (
                                description[:50]
                                + (
                                    '...'
                                    if len(description) > 50
                                    else ''
                                )
                            ),

                            'is_sample': test_case.get(
                                'is_sample',
                                False
                            ),

                            'full_input': input_data,
                            'full_output': output_data,
                            'full_description': description,
                        }
                    )

                context['all_test_cases'] = (
                    display_test_cases
                )

                # =====================================================
                # PREPARE PROBLEM DATA
                # =====================================================

                title = (
                    html_data['title']
                    or info.get(
                        'name',
                        f'Polygon Problem {polygon_id}'
                    )
                )

                slug = slugify(title)

                problem_statement = (
                    html_data['legend']
                )

                input_format = (
                    html_data['input_format']
                )

                output_format = (
                    html_data['output_format']
                )

                constraints = ''
                editorial = ''

                time_limit = info.get(
                    'timeLimit',
                    1000
                )

                memory_limit = info.get(
                    'memoryLimit',
                    256
                )

                checker_type = (
                    api._make_request(
                        'problem.checker',
                        {
                            'problemId': polygon_id
                        }
                    )
                    or 'ncmp'
                )

                logger.debug(
                    'checker_type=%s',
                    checker_type
                )

                if checker_type.startswith(
                    'std::'
                ):
                    checker_type = checker_type[5:]

                if checker_type.endswith(
                    '.cpp'
                ):
                    checker_type = checker_type[:-4]

                valid_checkers = [
                    'ncmp',
                    'fcmp',
                    'hcmp',
                    'lcmp',
                    'nyesno',
                    'rcmp4',
                    'rcmp6',
                    'rcmp9',
                    'wcmp',
                    'yesno'
                ]

                if checker_type not in valid_checkers:
                    checker_type = 'custom'

                custom_checker_info = (
                    api.get_custom_checker_info(
                        polygon_id
                    )
                )

                test_case_count = len(
                    api.get_test_cases(
                        polygon_id
                    )
                )

                logger.debug(
                    'test_case_count=%d',
                    test_case_count
                )

                notes = html_data['notes']

                context['fetched_problem'] = {

                    'polygon_id': polygon_id,

                    'title': title,

                    'slug': slug,

                    'difficulty': difficulty,

                    'problem_statement': (
                        problem_statement
                    ),

                    'input_format': input_format,

                    'output_format': output_format,

                    'constraints': constraints,

                    'editorial': editorial,

                    'time_limit': time_limit,

                    'memory_limit': memory_limit,

                    'checker_type': checker_type,

                    'custom_checker_info': (
                        custom_checker_info
                    ),

                    'test_case_count': (
                        test_case_count
                    ),

                    'notes': notes,
                }

                # =====================================================
                # HANDLE DATABASE MIGRATION
                # =====================================================

                if migrate_to_db:

                    if not difficulty:

                        context['error'] = (
                            "Please select a difficulty level "
                            "before migrating to database."
                        )

                        raise Exception(
                            context['error']
                        )

                    logger.info(
                        'Starting DB migration for polygon_id=%s',
                        polygon_id
                    )

                    problem_obj = (
                        Problem.objects.filter(
                            Q(polygon_id=polygon_id)
                        ).first()
                    )

                    if problem_obj:

                        logger.info(
                            'Updating existing Problem in DB'
                        )

                        problem_obj.title = title
                        problem_obj.slug = slug
                        problem_obj.difficulty = difficulty
                        problem_obj.problem_statement = (
                            problem_statement
                        )
                        problem_obj.input_format = (
                            input_format
                        )
                        problem_obj.output_format = (
                            output_format
                        )
                        problem_obj.constraints = (
                            constraints
                        )
                        problem_obj.editorial = (
                            editorial
                        )
                        problem_obj.time_limit = (
                            time_limit
                        )
                        problem_obj.memory_limit = (
                            memory_limit
                        )
                        problem_obj.checker_type = (
                            checker_type
                        )
                        problem_obj.test_case_count = (
                            test_case_count
                        )
                        problem_obj.polygon_id = (
                            polygon_id
                        )
                        problem_obj.notes = notes

                        problem_obj.save()

                        context['db_success'] = (
                            f"Problem '{title}' updated "
                            "in database successfully."
                        )

                        if custom_checker_info:

                            context['db_success'] += (
                                f" Custom checker "
                                f"'{custom_checker_info['name']}' "
                                "detected."
                            )

                    else:

                        logger.info(
                            'Creating new Problem in DB'
                        )

                        problem_obj = (
                            Problem.objects.create(
                                polygon_id=polygon_id,
                                title=title,
                                slug=slug,
                                difficulty=difficulty,
                                problem_statement=(
                                    problem_statement
                                ),
                                input_format=input_format,
                                output_format=output_format,
                                constraints=constraints,
                                editorial=editorial,
                                time_limit=time_limit,
                                memory_limit=memory_limit,
                                checker_type=checker_type,
                                test_case_count=(
                                    test_case_count
                                ),
                                notes=notes,
                            )
                        )

                        context['db_success'] = (
                            f"Problem '{title}' created "
                            "in database successfully."
                        )

                        if custom_checker_info:

                            context['db_success'] += (
                                f" Custom checker "
                                f"'{custom_checker_info['name']}' "
                                "detected."
                            )

                    # =================================================
                    # HANDLE TAGS
                    # =================================================

                    logger.info(
                        'Processing tags for problem'
                    )

                    logger.debug(
                        'Selected tags before processing: %s',
                        selected_tags
                    )

                    logger.debug(
                        'New tag before processing: %s',
                        new_tag
                    )

                    # Clear existing tags
                    problem_obj.extra_tags.clear()

                    logger.debug(
                        'Cleared existing tags for problem %s',
                        problem_obj.id
                    )

                    # Process selected tags
                    for tag_name in selected_tags:

                        if tag_name.strip():

                            logger.debug(
                                'Processing tag: %s',
                                tag_name
                            )

                            tag, created = (
                                ProblemTag.objects.get_or_create(
                                    tag_name=tag_name.strip()
                                )
                            )

                            problem_obj.extra_tags.add(
                                tag
                            )

                            if created:

                                logger.info(
                                    'Created new tag: %s',
                                    tag_name
                                )

                            else:

                                logger.debug(
                                    'Added existing tag: %s',
                                    tag_name
                                )

                    # Process new tag
                    if new_tag:

                        logger.debug(
                            'Processing new tag: %s',
                            new_tag
                        )

                        new_tag_obj, created = (
                            ProblemTag.objects.get_or_create(
                                tag_name=new_tag
                            )
                        )

                        problem_obj.extra_tags.add(
                            new_tag_obj
                        )

                        if created:

                            logger.info(
                                'Created new tag from input: %s',
                                new_tag
                            )

                        else:

                            logger.debug(
                                'Added existing tag from input: %s',
                                new_tag
                            )

                    final_tags = [
                        tag.tag_name
                        for tag in problem_obj.extra_tags.all()
                    ]

                    context['selected_tags'] = (
                        final_tags
                    )

                    logger.debug(
                        'Final tags after processing: %s',
                        final_tags
                    )

                    # =================================================
                    # GET TEST CASES FOR DATABASE MIGRATION
                    # =================================================

                    test_cases = (
                        api.get_test_cases_from_redis(
                            polygon_id
                        )
                    )

                    if test_cases is None:

                        logger.warning(
                            'Test cases not found in Redis, '
                            'fetching from Polygon'
                        )

                        test_cases = (
                            api.get_all_test_cases(
                                polygon_id
                            )
                        )

                        api.store_test_cases_in_redis(
                            polygon_id,
                            test_cases,
                            expiry_hours=0.5
                        )

                    else:

                        logger.info(
                            'Retrieved test cases from Redis '
                            'for test case migration to DB'
                        )

                    # =================================================
                    # UPDATE SAMPLE TEST CASES
                    # =================================================

                    sample_tests = [
                        test
                        for test in test_cases
                        if test.get(
                            'is_sample',
                            False
                        )
                    ]

                    existing_sample_cases = list(
                        SampleTestCase.objects.filter(
                            problem=problem_obj
                        ).order_by('order')
                    )

                    order = 1

                    for idx, test in enumerate(
                        sample_tests
                    ):

                        input_data = test.get(
                            'input',
                            ''
                        ).rstrip()

                        output_data = test.get(
                            'output',
                            ''
                        ).rstrip()

                        if input_data and output_data:

                            if idx < len(
                                existing_sample_cases
                            ):

                                stc = (
                                    existing_sample_cases[idx]
                                )

                                stc.input = input_data
                                stc.output = output_data
                                stc.order = order

                                stc.save()

                            else:

                                SampleTestCase.objects.create(
                                    problem=problem_obj,
                                    input=input_data,
                                    output=output_data,
                                    order=order
                                )

                            order += 1

                    # Refresh database problem
                    db_problem = Problem.objects.get(
                        pk=problem_obj.pk
                    )

                    context['db_problem'] = (
                        db_problem
                    )

                    # =================================================
                    # FETCH MAIN CORRECT SOLUTION
                    # =================================================

                    main_solution = None

                    try:

                        solutions = api._make_request(
                            'problem.solutions',
                            {
                                'problemId': polygon_id
                            }
                        )

                        logger.debug(
                            'Fetched solutions: %s',
                            solutions
                        )

                        if solutions:

                            main_solution_name = None

                            for solution in solutions:

                                if solution.get(
                                    'tag'
                                ) == 'MA':

                                    main_solution_name = (
                                        solution['name']
                                    )

                                    break

                            if (
                                not main_solution_name
                                and solutions
                            ):

                                main_solution_name = (
                                    solutions[0]['name']
                                )

                            if main_solution_name:

                                main_solution = (
                                    api._make_plain_request(
                                        'problem.viewSolution',
                                        {
                                            'problemId': polygon_id,
                                            'name': (
                                                main_solution_name
                                            )
                                        }
                                    )
                                )

                    except Exception as e:

                        logger.error(
                            'Error fetching solution: %s',
                            e
                        )

                        main_solution = None

                    context['main_solution'] = (
                        main_solution
                    )

                # =====================================================
                # MIGRATE TEST CASE DESCRIPTIONS TO DATABASE
                # =====================================================

                if migrate_test_cases_to_db:

                    problem_obj = (
                        Problem.objects.filter(
                            polygon_id=polygon_id
                        ).first()
                    )

                    if not problem_obj:

                        logger.warning(
                            'Problem not found in DB '
                            'for test case migration'
                        )

                        context['error'] = (
                            "Please migrate the problem "
                            "to the database first."
                        )

                        raise Exception(
                            context['error']
                        )

                    test_cases = (
                        api.get_test_cases_from_redis(
                            polygon_id
                        )
                    )

                    if test_cases is None:

                        logger.warning(
                            'Test cases not found in Redis, '
                            'fetching from Polygon'
                        )

                        test_cases = (
                            api.get_all_test_cases(
                                polygon_id
                            )
                        )

                        api.store_test_cases_in_redis(
                            polygon_id,
                            test_cases,
                            expiry_hours=0.5
                        )

                    else:

                        logger.info(
                            'Retrieved test cases from Redis '
                            'for test case migration to DB'
                        )

                    existing_test_cases = list(
                        ProblemTestCase.objects.filter(
                            problem=problem_obj
                        ).order_by('order')
                    )

                    order = 1

                    for idx, test in enumerate(
                        test_cases
                    ):

                        input_data = test.get(
                            'input',
                            ''
                        ).rstrip()

                        output_data = test.get(
                            'output',
                            ''
                        ).rstrip()

                        is_sample = test.get(
                            'is_sample',
                            False
                        )

                        description = test.get(
                            'description',
                            ''
                        )

                        if input_data and output_data:

                            truncated_input = (
                                input_data[:260]
                            )

                            truncated_output = (
                                output_data[:260]
                            )

                            if idx < len(
                                existing_test_cases
                            ):

                                ptc = (
                                    existing_test_cases[idx]
                                )

                                ptc.input = (
                                    truncated_input
                                )

                                ptc.output = (
                                    truncated_output
                                )

                                ptc.is_sample = (
                                    is_sample
                                )

                                ptc.description = (
                                    description
                                )

                                ptc.order = order

                                ptc.save()

                            else:

                                ProblemTestCase.objects.create(
                                    problem=problem_obj,
                                    is_sample=is_sample,
                                    input=truncated_input,
                                    output=truncated_output,
                                    description=description,
                                    order=order
                                )

                            order += 1

                    success_message = (
                        "Test cases description migrated "
                        "to database Successfully."
                    )

                    context['success'] = (
                        success_message
                    )

        except Exception as e:

            logger.error(
                'Exception in index view: %s',
                e,
                exc_info=True
            )

            context['error'] = (
                "Migration failed and all changes have "
                f"been rolled back. Reason: {str(e)}"
            )

            # Storage migration is provider-neutral.
            # Do not call the old Azure cleanup function.
            if (
                storage_uploaded
                and storage_problem_id
            ):

                logger.warning(
                    'Storage migration completed before rollback: '
                    'problem_id=%s',
                    storage_problem_id
                )

            # Clear cached test cases
            if polygon_id:

                api.clear_test_cases_from_redis(
                    polygon_id
                )

    # =============================================================
    # UPDATE SELECTED TAGS JSON
    # =============================================================

    if 'selected_tags' in context:

        context['selected_tags_json'] = json.dumps(
            context['selected_tags']
        )

    else:

        context['selected_tags_json'] = json.dumps(
            []
        )

    logger.info(
        'Exiting index view'
    )

    return render(
        request,
        'problems/index.html',
        context
    )