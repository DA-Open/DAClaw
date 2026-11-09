"""Adapter contract checks; does not import DAClaw or contact a model."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace

import runner


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = self.root / 'Template'
        self.dataset.mkdir()
        from openpyxl import Workbook
        book = Workbook()
        book.active['A1'] = 1
        book.save(self.dataset / 'input.xlsx')
        book.save(self.dataset / 'gold.xlsx')
        self.task = {'id': 'a', 'instruction': 'Set A1 to 2', 'spreadsheet_path': 'input.xlsx',
                     'golden_response_path': 'gold.xlsx', 'answer_position': 'Sheet!A1'}
        self.save([self.task])

    def save(self, tasks):
        runner.dump(self.dataset / 'dataset.json', tasks)

    def test_load_subset(self):
        self.assertEqual(runner.load_tasks(self.dataset, ids='a', gold=True), [self.task])

    def test_unknown_id(self):
        with self.assertRaises(ValueError):
            runner.load_tasks(self.dataset, ids='missing')

    def test_duplicate_id(self):
        self.save([self.task, self.task])
        with self.assertRaises(ValueError):
            runner.load_tasks(self.dataset)

    def test_unsafe_id(self):
        self.save([dict(self.task, id='../escape')])
        with self.assertRaises(ValueError):
            runner.load_tasks(self.dataset)

    def test_input_traversal(self):
        self.save([dict(self.task, spreadsheet_path='../outside.xlsx')])
        with self.assertRaises(ValueError):
            runner.load_tasks(self.dataset)

    def test_input_symlink(self):
        outside = self.root / 'outside.xlsx'
        outside.write_bytes(b'x')
        (self.dataset / 'link.xlsx').symlink_to(outside)
        with self.assertRaises(ValueError):
            runner.inside(self.dataset, 'link.xlsx')

    def test_missing_gold_only_required_at_scoring(self):
        (self.dataset / 'gold.xlsx').unlink()
        self.assertEqual(len(runner.load_tasks(self.dataset)), 1)
        with self.assertRaises(FileNotFoundError):
            runner.load_tasks(self.dataset, gold=True)

    def test_prompt_excludes_grading(self):
        work = runner.prepare(self.task, self.dataset, self.root / 'out')
        request = json.loads((work / 'request.json').read_text())
        self.assertNotIn('gold', json.dumps(request))
        self.assertNotIn('answer_position', request)
        self.assertEqual((work / 'input.xlsx').read_bytes(), (self.dataset / 'input.xlsx').read_bytes())
        self.assertFalse((work / 'gold.xlsx').exists())

    def test_signature_changes_with_input_and_model(self):
        before = runner.signature(self.task, self.dataset, {'model': 'a'})
        self.assertNotEqual(before, runner.signature(self.task, self.dataset, {'model': 'b'}))
        (self.dataset / 'input.xlsx').write_bytes(b'changed')
        self.assertNotEqual(before, runner.signature(self.task, self.dataset, {'model': 'a'}))

    def test_failed_worker_removes_stale_published_output(self):
        output = self.root / 'out'
        target = output / 'output_excel/a_output.xlsx'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'stale')
        args = Namespace(output=output, dataset=self.dataset, resume=False, timeout=1)
        with patch('runner.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 1
            result = runner.run_one(self.task, args, {})
        self.assertEqual(result['status'], 'error')
        self.assertFalse(target.exists())

    def test_missing_libreoffice_is_explicit(self):
        with patch('runner.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'LibreOffice missing'):
                runner.recalculate(self.dataset / 'input.xlsx')

    def test_visualization_does_not_use_cell_evaluator(self):
        with self.assertRaisesRegex(ValueError, 'Windows'):
            runner.evaluate([self.task], Namespace(category='Visualization'))


if __name__ == '__main__':
    unittest.main()
