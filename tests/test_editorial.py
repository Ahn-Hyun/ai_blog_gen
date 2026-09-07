import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_blog as blog
from editorial import EditorialError, check_edit, validate_evidence


class Writer:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    def generate(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.response


class EditorialChecks(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(quality_gate_revisions=0, anthropic_temperature=0,
            anthropic_max_tokens=1024, final_review_enabled=True, humanizer_enabled=True)
        self.sources = [{'url': 'https://example.org/report', 'excerpt': 'The cost was $10.7 million in 2025.'}]
        self.evidence = {'claims': [{'claim': 'Cost was $10.7 million in 2025',
            'source': self.sources[0]['url'], 'evidence_quote': self.sources[0]['excerpt'],
            'kind': 'fact', 'period': '2025', 'unit': 'USD million', 'region': 'Global'}]}

    def test_unicode_words_dates_and_names(self):
        for value in ['real estate—plagued', '2025–2026', 'investor’s return', 'São Paulo']:
            self.assertEqual(blog._normalize_text(value), value)

    def test_editor_cannot_change_date_or_drop_link(self):
        before = 'Cost: $10.7 in 2025. [JLL](https://example.org/report)'
        for after in [before.replace('2025', '2023'), before.split(' [')[0]]:
            with self.assertRaises(EditorialError):
                check_edit(before, after)
        check_edit(before, 'In 2025, cost was $10.7. [JLL](https://example.org/report)')

    def test_editor_cannot_change_table_or_code(self):
        for value in ['|Year|Cost|\n|---|---|\n|2025|10.7|\n', '`rate * principal`']:
            with self.assertRaises(EditorialError):
                check_edit(value, value.replace('Cost', 'Profit').replace('principal', 'balance'))

    def test_evidence_requires_real_quote_and_period(self):
        validate_evidence(self.evidence, self.sources)
        for key, value in [('source', 'https://invented.org'), ('evidence_quote', 'invented'),
                           ('period', 'unknown'), ('kind', 'inference')]:
            evidence = copy.deepcopy(self.evidence)
            evidence['claims'][0][key] = value
            with self.assertRaises(EditorialError):
                validate_evidence(evidence, self.sources)

    def test_numeric_claim_cannot_substitute_year(self):
        evidence = copy.deepcopy(self.evidence)
        evidence['claims'][0]['claim'] += '.'
        validate_evidence(evidence, self.sources)
        evidence['claims'][0]['claim'] = 'Cost was $10.7 million in 2023.'
        with self.assertRaises(EditorialError):
            validate_evidence(evidence, self.sources)

    def test_quality_reviewer_receives_source_and_current_date(self):
        from datetime import datetime, timezone
        writer = Writer()
        with patch.object(writer, 'generate', return_value='{"status":"pass","issues":[]}') as generate:
            blog._apply_quality_gate(self.config, writer, full_mdx='Draft', keyword='cost', sources=self.sources)
        prompt = str(generate.call_args)
        self.assertIn(self.sources[0]['excerpt'], prompt)
        self.assertIn(datetime.now(timezone.utc).date().isoformat(), prompt)

    def test_quality_gate_failures_do_not_return_publishable_text(self):
        for writer in [Writer('not json'), Writer('{"status":"revise"}'),
                       Writer('{"status":"pass","issues":["wrong date"]}'),
                       Writer(error=TimeoutError())]:
            with self.assertRaises(EditorialError):
                blog._apply_quality_gate(self.config, writer, full_mdx='Draft', keyword='cost')
        self.assertEqual(blog._apply_quality_gate(self.config, Writer('{"status":"pass","issues":[]}'),
                         full_mdx='Draft', keyword='cost'), 'Draft')

    def test_humanizer_failure_is_not_silent(self):
        with self.assertRaises(EditorialError):
            blog._apply_final_review(self.config, Writer('In 2023'), full_mdx='In 2025', keyword='cost')
        with self.assertRaises(EditorialError):
            blog._apply_final_review(self.config, Writer(error=TimeoutError()), full_mdx='Draft', keyword='cost')

    def test_source_audit_rejects_uncertainty_and_unavailability(self):
        context = {'sources': self.sources, 'evidence': self.evidence}
        for writer in [Writer('{}'), Writer('{"status":"reject","issues":["may changed to will"]}'),
                       Writer(error=TimeoutError())]:
            with self.assertRaises(EditorialError):
                blog._audit_final_article(self.config, writer, 'Draft', context)

    def test_outline_failure_cannot_restore_fixed_template(self):
        with self.assertRaises(EditorialError):
            blog._build_outline(self.config, Writer('{}'), keyword='cost', angle='scope', evidence_summary='facts')

    def test_multi_stage_rejection_does_not_call_fallback(self):
        config = SimpleNamespace(use_multi_agent=True)
        with patch.object(blog, '_describe_image_urls', return_value=[]), \
             patch.object(blog, '_generate_article_multi_agent', side_effect=EditorialError('bad evidence')), \
             patch.object(blog, '_build_content_prompt') as fallback:
            with self.assertRaises(EditorialError):
                blog._generate_post_for_topic(config, Writer(), Writer(), {'keyword': 'test'})
            fallback.assert_not_called()

    def test_failed_build_does_not_mark_topic_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.mdx'
            path.write_text('---\ndraft: false\n---\nBody')
            config = SimpleNamespace(anthropic_api_key='', anthropic_model_content='test',
                                     anthropic_model_meta='test', anthropic_timeout_sec=1)
            with patch.object(blog, '_load_state', return_value={}), \
                 patch.object(blog, '_save_state') as save, \
                 patch.object(blog, '_generate_post_for_topic', return_value=path), \
                 patch.object(blog, '_validate_and_repair_posts', return_value=False):
                with self.assertRaises(EditorialError):
                    blog._process_topics(config, topics=[{'keyword': 'test'}])
                save.assert_not_called()
                self.assertIn('draft: true', path.read_text())

    def test_completed_state_follows_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.mdx'
            path.write_text('---\ndraft: false\n---\nBody')
            config = SimpleNamespace(anthropic_api_key='', anthropic_model_content='test',
                                     anthropic_model_meta='test', anthropic_timeout_sec=1)
            events = []
            with patch.object(blog, '_load_state', return_value={}), \
                 patch.object(blog, '_save_state', side_effect=lambda state: events.append('save')), \
                 patch.object(blog, '_generate_post_for_topic', return_value=path), \
                 patch.object(blog, '_validate_and_repair_posts', side_effect=lambda *a, **k: events.append('validate') or True):
                blog._process_topics(config, topics=[{'keyword': 'test'}])
            self.assertEqual(events, ['validate', 'save'])

    def test_final_audit_sees_metadata_and_visuals_before_file_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(blog._build_config(), content_dir=root / 'blog',
                             hero_base_dir=root / 'images', post_draft=False)
            kwargs = dict(title='Test title', description='Test description', category=['stocks'],
                tags=['test'], body='Body', hero_alt='Conceptual illustration', image_prompt='test',
                reference_urls=[self.sources[0]['url']], chart_specs=[{'values': [10.7]}],
                inline_image_prompts=[], slug_hint='test', writer=Writer(),
                review_context={'sources': self.sources, 'evidence': self.evidence})
            def reject(config, writer, article, context):
                self.assertIn('Test title', article)
                self.assertIn('Caption from chart', article)
                self.assertIn('10.7', article)
                self.assertFalse(config.content_dir.exists())
                raise EditorialError('chart conflicts with evidence')
            with patch.object(blog, 'ROOT_DIR', root), \
                 patch.object(blog, '_materialize_inline_visuals', return_value=[{'block': 'Caption from chart', 'section_heading': ''}]), \
                 patch.object(blog, '_audit_final_article', side_effect=reject), \
                 patch.object(blog, '_generate_hero_image') as image:
                with self.assertRaises(EditorialError):
                    blog._write_post(config, **kwargs)
                image.assert_not_called()
            self.assertFalse(config.content_dir.exists())
            self.assertEqual(len(list((root / 'data/editorial-rejections').glob('*.mdx'))), 1)

    def test_unparsed_quality_gate_error_cannot_pass(self):
        config = SimpleNamespace(astro_root=Path('/tmp'))
        failed = SimpleNamespace(returncode=1, stdout='', stderr='tool unexpectedly failed')
        passed = SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(blog.subprocess, 'run', side_effect=[failed, passed]):
            self.assertFalse(blog._validate_and_repair_posts(config, Writer(),
                             post_paths=[Path('article.mdx')], max_rounds=0))

    def test_openai_client_sends_reasoning_effort(self):
        calls = []

        class Responses:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(output_text='{"ok": true}')

        class Client:
            def __init__(self, **kwargs):
                self.responses = Responses()

        with patch.object(blog, 'OpenAI', Client):
            client = blog.OpenAIResponsesClient('key', 'gpt-5.5', 10, 'high')
            self.assertEqual(client.generate(instructions='i', input_text='x'), '{"ok": true}')

        self.assertEqual(calls[0]['model'], 'gpt-5.5')
        self.assertEqual(calls[0]['reasoning'], {'effort': 'high'})

    def test_gemini_3_payload_omits_temperature(self):
        seen = []
        client = blog.ClaudeClient('key', 'gemini-3.8-flash', 1)

        with patch.object(client, '_post', side_effect=lambda payload: seen.append(payload) or {
            'candidates': [{'content': {'parts': [{'text': 'ok'}]}}],
        }):
            self.assertEqual(client.generate('x', temperature=0.6, max_tokens=20), 'ok')

        self.assertEqual(seen[0]['generationConfig'], {'maxOutputTokens': 20})

    def test_legacy_gemini_payload_keeps_temperature(self):
        client = blog.ClaudeClient('key', 'gemini-2.5-flash', 1)
        self.assertEqual(
            client._generation_config(temperature=0.6, max_tokens=20),
            {'maxOutputTokens': 20, 'temperature': 0.6},
        )

    def test_truncated_gemini_output_is_rejected(self):
        with self.assertRaises(EditorialError):
            blog.ClaudeClient._extract_text({'candidates': [{
                'finishReason': 'MAX_TOKENS', 'content': {'parts': [{'text': 'Partial article'}]},
            }]})

    def test_section_failure_cannot_publish_partial_article(self):
        config = SimpleNamespace(content_language='English', anthropic_temperature=0,
                                 anthropic_max_tokens=1024)
        for second in ['', TimeoutError()]:
            with patch.object(Writer, 'generate', side_effect=['First section', second]):
                with self.assertRaises(EditorialError):
                    blog._write_sections(config, Writer(), outline={'sections': [
                        {'heading': 'Context'}, {'heading': 'Risks'}]}, evidence={}, sources=[])

    def test_empty_discovery_does_not_report_success(self):
        config = blog._build_config()
        for run in [blog.run_daily_impact, blog.run_weekly_major_events]:
            with patch.object(blog, '_load_state', return_value={}), \
                 patch.object(blog, '_save_state') as save, \
                 patch.object(blog, '_gather_grounded_daily_discovery_sources', return_value=[]), \
                 patch.object(blog, '_gather_raw_sources_for_queries', return_value=[]):
                with self.assertRaises(EditorialError):
                    run(config, force=True)
                save.assert_not_called()

    def test_dated_rss_does_not_discard_direct_source_candidates(self):
        from datetime import datetime, timezone
        config = replace(blog._build_config(), search_web_enabled=True, tavily_api_key='test',
                         search_rss_enabled=True)
        direct = {'url': 'https://example.org/release', 'published_at': None}
        dated = [{'url': f'https://news.google.com/article/{i}',
                  'published_at': '2026-09-06T12:00:00Z'} for i in range(3)]
        with patch.object(blog, '_search_web_tavily', return_value=[direct]), \
             patch.object(blog, '_search_news_rss', return_value=dated):
            found = blog._collect_candidates_for_queries(config, queries=['rates'], region='US',
                language='English', window_start=datetime(2026,9,1,tzinfo=timezone.utc),
                window_end=datetime(2026,9,7,tzinfo=timezone.utc))
        self.assertEqual(found[:3], dated)
        self.assertIn(direct, found)

    def test_search_snippet_is_not_treated_as_fetched_evidence(self):
        config = replace(blog._build_config(), tavily_api_key='test')
        with patch.object(blog, '_extract_web_content_tavily', return_value=[]):
            self.assertEqual(blog._fetch_sources_from_candidates([{
                'url':'https://example.org/release', 'snippet':'A search preview, not a fetched page.'
            }], config), [])

    def test_evidence_repair_must_still_pass_original_checks(self):
        invalid = copy.deepcopy(self.evidence)
        invalid['claims'][0]['evidence_quote'] = 'Invented quote'
        for repaired, approved in [(self.evidence, True), (invalid, False)]:
            with patch.object(Writer, 'generate', side_effect=[json.dumps(invalid), json.dumps(repaired)]) as call:
                if approved:
                    self.assertEqual(blog._build_evidence_from_sources(self.config, Writer(), self.sources)['claims'], self.evidence['claims'])
                else:
                    with self.assertRaises(EditorialError):
                        blog._build_evidence_from_sources(self.config, Writer(), self.sources)
                self.assertEqual(call.call_count, 2)

    def test_unsupported_claim_is_omitted_without_weakening_evidence_checks(self):
        mixed = copy.deepcopy(self.evidence)
        mixed['claims'].append({**mixed['claims'][0], 'evidence_quote': 'Not in the source'})
        with patch.object(Writer, 'generate', return_value=json.dumps(mixed)):
            result = blog._build_evidence_from_sources(self.config, Writer(), self.sources)
        self.assertEqual(result['claims'], self.evidence['claims'])
        validate_evidence(result, self.sources)


if __name__ == '__main__':
    unittest.main()
