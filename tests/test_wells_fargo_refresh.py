import asyncio
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock,Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import wells_fargo_async as source


class Response:
    def __init__(self,status=200,html='',url='https://fa.wellsfargoadvisors.com/example/'):
        self.status=status;self.html=html;self.url=url
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    async def text(self):return self.html
    def raise_for_status(self):
        if self.status>=400:raise AssertionError('Denial should be checked first')


class WellsEvidenceTests(unittest.TestCase):
    def test_evidence_write_failure_is_fatal_not_a_retryable_empty_page(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(Path,'write_bytes',side_effect=OSError('storage unavailable')):
            with self.assertRaises(source.SourceEvidenceError):
                source.retain_page_observation(Path(folder)/'pages.jsonl','https://fa.wellsfargoadvisors.com/example/','https://fa.wellsfargoadvisors.com/example/',200,'<h1>Alice Perez</h1>')

    def test_retained_decoded_html_reproduces_exact_parser_input(self):
        with tempfile.TemporaryDirectory() as folder:
            journal=Path(folder)/'pages.jsonl';journal.touch()
            html='<h1>Ren\u00e9e Smith</h1>'
            source.retain_page_observation(journal,'https://fa.wellsfargoadvisors.com/example/','https://fa.wellsfargoadvisors.com/example/',200,html)
            entry=json.loads(journal.read_text(encoding='utf8'))
            body=gzip.decompress(Path(entry['htmlFile']).read_bytes())
            self.assertEqual(html,body.decode('utf8'))
            self.assertEqual(hashlib.sha256(body).hexdigest(),entry['decodedHtmlSha256'])


class WellsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_denial_is_recorded_and_never_retried(self):
        for status in [401,403,429]:
            session=Mock();session.get.return_value=Response(status)
            capture=Mock()
            with self.assertRaises(PermissionError):await source.parse_advisor_page(session,'https://fa.wellsfargoadvisors.com/example/',capture)
            session.get.assert_called_once();self.assertEqual(status,capture.call_args.args[2])

    async def test_sitemap_denial_never_retries(self):
        session=Mock();session.get.return_value=Response(429)
        with self.assertRaises(PermissionError):await source.fetch_sitemap(session,'https://fa.wellsfargoadvisors.com/sitemap.xml',{})
        session.get.assert_called_once()

    async def test_related_team_denial_stops_instead_of_returning_empty(self):
        html='<h1>Alice Perez</h1><a href="mailto:alice.perez@example.invalid">Email</a><a href="our-team.htm">Our Team</a>'
        session=Mock();session.get.side_effect=[Response(200,html),Response(403)]
        with self.assertRaises(PermissionError):await source.parse_advisor_page(session,'https://fa.wellsfargoadvisors.com/example/')
        self.assertEqual(2,session.get.call_count)

    async def test_blank_or_cross_host_page_cannot_become_a_person(self):
        for response in [Response(200,''),Response(200,'<h1>Alice Perez</h1><a href="mailto:alice.perez@example.invalid">Email</a>','https://elsewhere.invalid/')]:
            session=Mock();session.get.return_value=response
            self.assertIsNone(await source.parse_advisor_page(session,'https://fa.wellsfargoadvisors.com/example/'))

    async def test_first_denial_cancels_queued_work_and_preserves_prior_roster(self):
        xml='<urlset>'+''.join('<url><loc>https://fa.wellsfargoadvisors.com/p'+str(i)+'/</loc></url>' for i in range(1000))+'</urlset>'
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'roster.csv';target.write_text('prior',encoding='utf8')
            with patch.object(source,'OUTPUT_FILE',str(target)),patch.object(source,'OUTPUT_DIR',folder), \
                 patch.object(source,'scratch_path',return_value=Path(folder)/'journal.jsonl'), \
                 patch.object(source,'fetch_sitemap',new=AsyncMock(return_value=xml)), \
                 patch.object(source,'parse_advisor_page',new=AsyncMock(side_effect=PermissionError('denied'))) as parser, \
                 patch.object(source.aiohttp,'ClientSession'),patch.object(source.asyncio,'sleep',new=AsyncMock()):
                with self.assertRaises(PermissionError):await source.process_sitemap('https://fa.wellsfargoadvisors.com/sitemap.xml',max_concurrent=1)
            self.assertEqual(1,parser.await_count);self.assertEqual('prior',target.read_text(encoding='utf8'))


if __name__=='__main__':unittest.main()
