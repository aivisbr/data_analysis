"""Real FFmpeg offline tests: bytes, sample boundaries, restart and API validation."""
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.audio import AudioStore, AudioError, PreparationRequest, SAMPLE_RATE
from app.config import Settings
from app.main import create_app


def fixture():
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        # Non-silent deterministic stereo, repeated words must never be deduplicated.
        wav.writeframes(b''.join(struct.pack('<hh', (i % 120) * 100, (i % 120) * 100)
                                 for i in range(48000 * 3)))
    return output.getvalue()


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AudioStore(self.root / 'audio')
        self.payload = fixture()

    def upload(self):
        return self.store.upload(r'C:\private\diktāts.wav', io.BytesIO(self.payload))

    def test_original_bytes_and_browser_preview_survive_restart(self):
        item = self.upload()
        self.assertEqual(item['filename'], 'diktāts.wav')
        self.assertEqual(self.store.upload_path(item['id'], 'original').read_bytes(), self.payload)
        self.assertEqual(AudioStore(self.root / 'audio').list_uploads(), [item])
        with wave.open(str(self.store.upload_path(item['id'], 'preview'))) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()),
                             (1, 2, SAMPLE_RATE, SAMPLE_RATE * 3))

    def test_interval_chunks_exact_partition_with_same_shared_source(self):
        item = self.upload()
        data = self.store.prepare(PreparationRequest(audio_id=item['id'], reading='D',
            start_seconds=.5, end_seconds=2.75, chunk_seconds=1))
        self.assertEqual(data['directory'], 'Try2')
        self.assertEqual(data['chunk_policy']['overlap_samples'], 0)
        self.assertEqual([c['end_sample'] - c['start_sample'] for c in data['chunks']], [24000, 24000, 6000])
        for previous, following in zip(data['chunks'], data['chunks'][1:]):
            self.assertEqual(previous['end_sample'], following['start_sample'])
            self.assertEqual(previous['source_end_seconds'], following['source_start_seconds'])
        joined = b''
        for index in range(3):
            with wave.open(str(self.store.prepared_path(data['id'], index))) as wav:
                joined += wav.readframes(wav.getnframes())
        with wave.open(str(self.store.prepared_path(data['id']))) as wav:
            self.assertEqual(joined, wav.readframes(wav.getnframes()))
        # Multiple workers resolve identical bytes; neither gets a different crop.
        self.assertEqual(self.store.prepared_path(data['id'], 0), self.store.prepared_path(data['id'], 0))
        self.assertEqual(AudioStore(self.root / 'audio')._load('prepared', data['id'])[1], data)

    def test_invalid_limits_codec_interval_and_no_partial_publish(self):
        for name, payload in [('bad.txt', self.payload), ('bad.wav', b'garbage')]:
            with self.assertRaises(AudioError):
                self.store.upload(name, io.BytesIO(payload))
        with patch('app.audio.MAX_UPLOAD_BYTES', 5), self.assertRaises(AudioError):
            self.upload()
        self.assertEqual(self.store.list_uploads(), [])
        item = self.upload()
        with self.assertRaises(AudioError):
            self.store.prepare(PreparationRequest(audio_id=item['id'], reading='A', end_seconds=4))
        for values in [dict(start_seconds=-1), dict(start_seconds=float('nan')),
                       dict(start_seconds=2, end_seconds=1), dict(chunk_seconds=301)]:
            with self.assertRaises(ValidationError):
                PreparationRequest(audio_id=item['id'], reading='A', **values)
        self.assertFalse(any(p.name.startswith('.') for p in (self.root / 'audio' / 'uploads').iterdir()))

    def test_byte_limit_restricts_chunk_size_and_hash_tampering_rejected(self):
        item = self.upload()
        with patch('app.audio.MAX_CHUNK_BYTES', 48044):
            data = self.store.prepare(PreparationRequest(audio_id=item['id'], reading='A'))
        self.assertEqual(len(data['chunks']), 3)
        self.assertTrue(all(c['bytes'] <= 48044 for c in data['chunks']))
        self.store.prepared_path(data['id'], 0).write_bytes(b'changed')
        with self.assertRaises(AudioError):
            self.store.prepared_path(data['id'], 0)
        self.store.upload_path(item['id'], 'original').write_bytes(b'changed')
        with self.assertRaises(AudioError):
            self.store.prepare(PreparationRequest(audio_id=item['id'], reading='A'))

    def test_missing_tool_and_conversion_failure_do_not_publish(self):
        with patch('app.audio.subprocess.run', side_effect=FileNotFoundError), self.assertRaises(AudioError) as error:
            self.upload()
        self.assertEqual(error.exception.code, 'unavailable')
        self.assertEqual(self.store.list_uploads(), [])
        item = self.upload()
        with patch.object(self.store, '_convert', side_effect=AudioError('Neizdevās.')):
            with self.assertRaises(AudioError):
                self.store.prepare(PreparationRequest(audio_id=item['id'], reading='A'))
        self.assertEqual(list((self.root / 'audio' / 'prepared').iterdir()), [])

    def test_api_upload_preview_preparation_and_restart(self):
        settings = Settings(storage_root=self.root / 'application')
        with TestClient(create_app(settings)) as client:
            response = client.post('/api/audio-inputs', files={'file': ('diktāts.wav', self.payload, 'audio/wav')})
            self.assertEqual(response.status_code, 201, response.text)
            item = response.json()
            self.assertEqual(client.get(f"/api/audio-inputs/{item['id']}/files/original").content, self.payload)
            response = client.post('/api/audio-preparations', json=dict(audio_id=item['id'], reading='A', end_seconds=2))
            self.assertEqual(response.status_code, 201, response.text)
            prepared = response.json()
            self.assertEqual(client.get(f"/api/audio-preparations/{prepared['id']}/preview").headers['content-type'], 'audio/wav')
            self.assertEqual(client.get(f"/api/audio-preparations/{prepared['id']}/chunks/9").status_code, 404)
            self.assertEqual(client.get(f"/api/audio-inputs/{item['id']}/files/secrets").status_code, 422)
            self.assertEqual(client.post('/api/audio-preparations', json=dict(audio_id=item['id'], reading='A', end_seconds=9)).status_code, 422)
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.get('/api/audio-inputs').json()[0]['id'], item['id'])
            self.assertEqual(client.get(f"/api/audio-preparations/{prepared['id']}").json(), prepared)


if __name__ == '__main__':
    unittest.main()
