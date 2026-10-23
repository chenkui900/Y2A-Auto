import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modules import task_manager as tm


class SubtitleEmbedOnlyFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.downloads_dir = os.path.join(self.tmpdir, 'downloads')
        self.task_id = 'task-embed-only'
        self.task_dir = os.path.join(self.downloads_dir, self.task_id)
        os.makedirs(self.task_dir, exist_ok=True)
        self.video_path = os.path.join(self.task_dir, 'video.mp4')
        self.embedded_path = os.path.join(self.task_dir, 'video_with_subtitle.mp4')
        with open(self.video_path, 'wb') as fh:
            fh.write(b'fake video')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _write_srt(path, text='Hello world'):
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n")

    def _run_with_task_patches(self, task, callback):
        def fake_get_task(task_id):
            self.assertEqual(task_id, self.task_id)
            return dict(task)

        def fake_update_task(task_id, **kwargs):
            self.assertEqual(task_id, self.task_id)
            task.update({key: value for key, value in kwargs.items() if key != 'silent'})
            return True

        with patch.object(tm, 'DOWNLOADS_DIR', self.downloads_dir), \
             patch.object(tm, 'get_task', side_effect=fake_get_task), \
             patch.object(tm, 'update_task', side_effect=fake_update_task):
            return callback()

    def test_translate_subtitle_burns_original_subtitle_when_translation_disabled(self):
        subtitle_path = os.path.join(self.task_dir, 'video.en.srt')
        self._write_srt(subtitle_path)
        task = {
            'id': self.task_id,
            'status': tm.TASK_STATES['READY_FOR_UPLOAD'],
            'video_path_local': self.video_path,
        }
        processor = tm.TaskProcessor({
            'SUBTITLE_TRANSLATION_ENABLED': False,
            'SUBTITLE_EMBED_IN_VIDEO': True,
            'SPEECH_RECOGNITION_ENABLED': False,
        })
        processor._embed_subtitle_in_video = MagicMock(return_value=self.embedded_path)

        with patch(
            'modules.subtitle_translator.create_translator_from_config',
            side_effect=AssertionError('translator should not be created'),
        ):
            result = self._run_with_task_patches(
                task,
                lambda: processor._translate_subtitle(self.task_id, MagicMock()),
            )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_called_once_with(
            self.task_id,
            self.video_path,
            subtitle_path,
            unittest.mock.ANY,
        )
        self.assertEqual(task['video_path_local'], self.embedded_path)
        self.assertEqual(task['subtitle_path_original'], subtitle_path)
        self.assertIsNone(task['subtitle_path_translated'])

    def test_prepare_upload_burns_existing_subtitle_when_translation_disabled(self):
        subtitle_path = os.path.join(self.task_dir, 'video.en.srt')
        self._write_srt(subtitle_path)
        task = {
            'id': self.task_id,
            'status': tm.TASK_STATES['READY_FOR_UPLOAD'],
            'video_path_local': self.video_path,
        }
        processor = tm.TaskProcessor({
            'SUBTITLE_TRANSLATION_ENABLED': False,
            'SUBTITLE_EMBED_IN_VIDEO': True,
            'SPEECH_RECOGNITION_ENABLED': False,
        })
        processor._embed_subtitle_in_video = MagicMock(return_value=self.embedded_path)

        result = self._run_with_task_patches(
            task,
            lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()),
        )

        self.assertEqual(result['video_path_local'], self.embedded_path)
        processor._embed_subtitle_in_video.assert_called_once_with(
            self.task_id,
            self.video_path,
            subtitle_path,
            unittest.mock.ANY,
        )
        self.assertEqual(task['subtitle_path_original'], subtitle_path)
        self.assertIsNone(task['subtitle_path_translated'])


if __name__ == '__main__':
    unittest.main()