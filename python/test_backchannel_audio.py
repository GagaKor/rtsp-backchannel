import hashlib
import json
import math
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

import backchannel_audio
from tools import compare_pcma_encoders


ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPARE_TOOL = ROOT / "tools" / "compare_pcma_encoders.py"


def pack_s16(samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def write_known_wav(path):
    samples = [
        max(-32768, min(32767, ((index * 977) % 65536) - 32768))
        for index in range(800)
    ]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(pack_s16(samples))
    return samples


@unittest.skipUnless(backchannel_audio.ffmpeg_available(), "ffmpeg is unavailable")
class FfmpegAudioTests(unittest.TestCase):
    def test_decode_argv_is_exact_and_explicit(self):
        path = pathlib.Path("input.wav")

        self.assertEqual(
            backchannel_audio.build_decode_argv(path, 8000),
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "input.wav",
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-af",
                (
                    "aresample=8000:resampler=swr:filter_size=32:phase_shift=10:"
                    "linear_interp=1:exact_rational=1:cutoff=0.97:"
                    "dither_method=none:osf=s16:ochl=mono"
                ),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "-fs",
                str(backchannel_audio.MAX_DECODED_S16_BYTES + 1),
                "pipe:1",
            ],
        )

    def test_terminal_ffmpeg_argv_has_volume_but_no_resample(self):
        argv = backchannel_audio.build_ffmpeg_pcma_argv(8000, 0.05, 1600)

        self.assertEqual(
            argv,
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-af",
                "volume=0.05",
                "-c:a",
                "pcm_alaw",
                "-f",
                "alaw",
                "-fs",
                "801",
                "pipe:1",
            ],
        )
        self.assertNotIn("aresample", " ".join(argv))

    def test_known_wav_decodes_once_and_both_encoders_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = pathlib.Path(directory) / "known.wav"
            write_known_wav(wav_path)

            decoded_runs = []
            ffmpeg_runs = []
            gst_runs = []
            for _ in range(3):
                decoded = backchannel_audio.decode_source(wav_path, 8000)
                decoded_runs.append(hashlib.sha256(decoded).hexdigest())
                ffmpeg = backchannel_audio.encode_pcma_ffmpeg(decoded, 0.05, 8000)
                gst = backchannel_audio.encode_pcma_gst_compatible(decoded, 0.05)
                ffmpeg_runs.append(hashlib.sha256(ffmpeg).hexdigest())
                gst_runs.append(hashlib.sha256(gst).hexdigest())
                self.assertEqual(len(decoded), 1600)
                self.assertEqual(len(ffmpeg), 800)
                self.assertEqual(len(gst), 800)

            self.assertEqual(len(set(decoded_runs)), 1)
            self.assertEqual(len(set(ffmpeg_runs)), 1)
            self.assertEqual(len(set(gst_runs)), 1)

    def test_decode_errors_are_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "missing.wav"
            with self.assertRaisesRegex(ValueError, "source file does not exist"):
                backchannel_audio.decode_source(missing, 8000)

    def test_source_size_is_checked_before_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "large.wav"
            source.write_bytes(b"x" * 4)
            with patch.object(backchannel_audio, "MAX_SOURCE_FILE_BYTES", 3), patch.object(
                backchannel_audio.subprocess, "Popen"
            ) as popen:
                with self.assertRaisesRegex(ValueError, "source file.*exceeds"):
                    backchannel_audio.decode_source(source, 8000)
            popen.assert_not_called()


class BoundedProcessRunnerTests(unittest.TestCase):
    def run_recorded(self, argv, **kwargs):
        real_popen = subprocess.Popen
        processes = []

        def launch(*args, **popen_kwargs):
            process = real_popen(*args, **popen_kwargs)
            processes.append(process)
            return process

        with patch.object(backchannel_audio.subprocess, "Popen", side_effect=launch):
            try:
                result = backchannel_audio._run_ffmpeg(argv, **kwargs)
            except BaseException as error:
                return None, error, processes
        return result, None, processes

    def assert_reaped(self, process):
        self.assertIsNotNone(process.poll())
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)

    def test_drains_split_output_and_input_concurrently(self):
        input_data = b"input-block-" * 16384
        chunk_size = 64 * 1024
        script = (
            "import os,sys\n"
            f"chunk=b'O'*{chunk_size}\n"
            "for _ in range(3): os.write(1,chunk)\n"
            "os.write(2,b'split diagnostic')\n"
            "data=sys.stdin.buffer.read()\n"
            "for offset in range(0,len(data),8192): "
            "os.write(1,data[offset:offset+8192])\n"
        )

        output, error, processes = self.run_recorded(
            [sys.executable, "-c", script],
            input_data=input_data,
            max_input_bytes=len(input_data),
            max_output_bytes=3 * chunk_size + len(input_data),
            max_stderr_bytes=64,
            context="split helper",
        )

        self.assertIsNone(error)
        self.assertEqual(output, b"O" * (3 * chunk_size) + input_data)
        self.assertEqual(len(processes), 1)
        self.assert_reaped(processes[0])

    def test_stdout_overflow_terminates_promptly_and_reaps_child(self):
        script = (
            "import os,time\n"
            "os.write(2,b'diagnostic-prefix')\n"
            "os.write(1,b'X'*4096)\n"
            "time.sleep(10)\n"
        )
        started = time.monotonic()
        with patch.object(backchannel_audio, "FFMPEG_TIMEOUT_SECONDS", 0.5):
            _, error, processes = self.run_recorded(
                [sys.executable, "-c", script],
                input_data=None,
                max_input_bytes=0,
                max_output_bytes=128,
                max_stderr_bytes=64,
                context="stdout helper",
            )

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("stdout exceeds 128 byte limit", str(error))
        self.assertIn("diagnostic-prefix", str(error))
        self.assertLess(len(str(error)), 512)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(processes), 1)
        self.assert_reaped(processes[0])

    def test_stderr_overflow_keeps_bounded_prefix_and_reaps_child(self):
        script = (
            "import os,time\n"
            "os.write(2,b'PREFIX-'+b'E'*4096)\n"
            "time.sleep(10)\n"
        )
        started = time.monotonic()
        with patch.object(backchannel_audio, "FFMPEG_TIMEOUT_SECONDS", 0.5):
            _, error, processes = self.run_recorded(
                [sys.executable, "-c", script],
                input_data=None,
                max_input_bytes=0,
                max_output_bytes=128,
                max_stderr_bytes=32,
                context="stderr helper",
            )

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("stderr exceeds 32 byte limit", str(error))
        self.assertIn("PREFIX-", str(error))
        self.assertLess(len(str(error)), 512)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(processes), 1)
        self.assert_reaped(processes[0])

    def test_rejects_oversized_input_before_launch(self):
        with patch.object(backchannel_audio.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(ValueError, "input exceeds 2 byte limit"):
            backchannel_audio._run_ffmpeg(
                [sys.executable, "-c", "pass"],
                input_data=b"123",
                max_input_bytes=2,
                max_output_bytes=0,
                max_stderr_bytes=0,
                context="input helper",
            )

        popen.assert_not_called()


class AlawTests(unittest.TestCase):
    def test_fixed_vectors_include_sign_segments_and_clipping(self):
        vectors = {
            -32768: 0x2A,
            -32636: 0x2A,
            -4096: 0x05,
            -256: 0x45,
            -1: 0x55,
            0: 0xD5,
            1: 0xD5,
            255: 0xDA,
            256: 0xC5,
            4095: 0x9A,
            4096: 0x85,
            32635: 0xAA,
            32767: 0xAA,
        }

        for sample, expected in vectors.items():
            with self.subTest(sample=sample):
                self.assertEqual(backchannel_audio.linear_to_alaw(sample), expected)

    def test_exhaustive_s16_digest_matches_fixed_gstreamer_oracle(self):
        encoded = bytes(
            backchannel_audio.linear_to_alaw(sample)
            for sample in range(-32768, 32768)
        )

        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            "61ab4ea19c31b12928e2b51176bd343304bde4314e26a84aa52a71e46942b893",
        )

    def test_encoder_accepts_s16le_and_emits_one_byte_per_sample(self):
        samples = [-32768, -1, 0, 1, 32767]
        encoded = backchannel_audio.encode_alaw_s16le(pack_s16(samples))

        self.assertEqual(len(encoded), len(samples))
        self.assertEqual(encoded, bytes(map(backchannel_audio.linear_to_alaw, samples)))

    def test_decoder_fixed_vectors_and_error_metrics(self):
        self.assertEqual(backchannel_audio.decode_alaw_byte(0xD5), 8)
        self.assertEqual(backchannel_audio.decode_alaw_byte(0x55), -8)
        self.assertEqual(backchannel_audio.decode_alaw_byte(0xAA), 32256)
        self.assertEqual(backchannel_audio.decode_alaw_byte(0x2A), -32256)

        metrics = backchannel_audio.decoded_error_metrics(b"\xd5\xaa", b"\xd5\x2a")
        self.assertEqual(metrics["differing_bytes"], 1)
        self.assertEqual(metrics["differing_percent"], 50.0)
        self.assertGreater(metrics["decoded_error_rms"], 0)
        self.assertTrue(math.isfinite(metrics["decoded_error_dbfs"]))
        self.assertTrue(math.isfinite(metrics["signal_rms"]))
        self.assertTrue(math.isfinite(metrics["snr_db"]))

    def test_identical_silence_metrics_are_strict_json_compatible(self):
        metrics = backchannel_audio.decoded_error_metrics(b"\xd5", b"\xd5")

        self.assertEqual(metrics["decoded_error_rms"], 0.0)
        self.assertIsNone(metrics["decoded_error_dbfs"])
        self.assertIsNone(metrics["snr_db"])
        json.dumps(metrics, allow_nan=False)

    def test_rejects_values_outside_s16_and_malformed_buffers(self):
        for value in (-32769, 32768, 1.5, True, "0"):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                backchannel_audio.linear_to_alaw(value)
        with self.assertRaisesRegex(ValueError, "even number of bytes"):
            backchannel_audio.encode_alaw_s16le(b"\x00")


class Q11VolumeTests(unittest.TestCase):
    def test_volume_point_zero_five_maps_to_gain_102(self):
        self.assertEqual(backchannel_audio.volume_to_q11(0.05), 102)

    def test_signed_multiply_uses_arithmetic_right_shift_and_clips(self):
        gain = backchannel_audio.volume_to_q11(0.05)
        vectors = {
            -32768: -1632,
            -2049: -103,
            -2048: -102,
            -2047: -102,
            -21: -2,
            -20: -1,
            -1: -1,
            0: 0,
            1: 0,
            20: 0,
            21: 1,
            2047: 101,
            2048: 102,
            32767: 1631,
        }
        for sample, expected in vectors.items():
            with self.subTest(sample=sample):
                self.assertEqual(
                    backchannel_audio.apply_q11_volume_sample(sample, gain), expected
                )

        self.assertEqual(backchannel_audio.apply_q11_volume_sample(-32768, 2048), -32768)
        self.assertEqual(backchannel_audio.apply_q11_volume_sample(32767, 2048), 32767)

    def test_amplification_clips_above_and_below_s16_boundaries(self):
        double_gain = 2 * backchannel_audio.Q11_UNITY

        self.assertEqual(
            backchannel_audio.apply_q11_volume_sample(16383, double_gain), 32766
        )
        self.assertEqual(
            backchannel_audio.apply_q11_volume_sample(16384, double_gain), 32767
        )
        self.assertEqual(
            backchannel_audio.apply_q11_volume_sample(-16383, double_gain), -32766
        )
        self.assertEqual(
            backchannel_audio.apply_q11_volume_sample(-16384, double_gain), -32768
        )
        self.assertEqual(
            backchannel_audio.apply_q11_volume_sample(-16385, double_gain), -32768
        )

    def test_gain_buffer_helper_clips_each_sample_safely(self):
        source = pack_s16([16383, 16384, -16383, -16384, -16385])

        self.assertEqual(
            backchannel_audio.apply_q11_gain_s16le(
                source, 2 * backchannel_audio.Q11_UNITY
            ),
            pack_s16([32766, 32767, -32766, -32768, -32768]),
        )

    def test_buffer_volume_and_encoding_share_the_same_samples(self):
        source = pack_s16([-32768, -21, -1, 0, 1, 21, 32767])
        scaled = backchannel_audio.apply_q11_volume_s16le(source, 0.05)
        expected = pack_s16([-1632, -2, -1, 0, 0, 1, 1631])

        self.assertEqual(scaled, expected)
        self.assertEqual(
            backchannel_audio.encode_pcma_gst_compatible(source, 0.05),
            backchannel_audio.encode_alaw_s16le(expected),
        )

    def test_rejects_nonfinite_out_of_range_volume_and_invalid_samples(self):
        for volume in (-0.01, 1.01, float("nan"), float("inf"), True, "0.5"):
            with self.subTest(volume=volume), self.assertRaises((TypeError, ValueError)):
                backchannel_audio.volume_to_q11(volume)
        for sample in (-32769, 32768, 0.5, True):
            with self.subTest(sample=sample), self.assertRaises((TypeError, ValueError)):
                backchannel_audio.apply_q11_volume_sample(sample, 102)
        self.assertEqual(
            backchannel_audio.apply_q11_volume_sample(
                1, backchannel_audio.MAX_Q11_GAIN
            ),
            10,
        )
        for gain in (
            -1,
            backchannel_audio.MAX_Q11_GAIN + 1,
            1.5,
            True,
        ):
            with self.subTest(gain=gain), self.assertRaises((TypeError, ValueError)):
                backchannel_audio.apply_q11_volume_sample(0, gain)


@unittest.skipUnless(backchannel_audio.ffmpeg_available(), "ffmpeg is unavailable")
class CompareCliTests(unittest.TestCase):
    def test_direct_cli_writes_atomic_json_metrics_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            source = directory / "known.wav"
            output = directory / "metrics.json"
            write_known_wav(source)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(COMPARE_TOOL),
                    "--file",
                    str(source),
                    "--volume",
                    "0.05",
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["sample_rate"], 8000)
            self.assertEqual(report["volume"], 0.05)
            self.assertEqual(report["source"]["bytes"], source.stat().st_size)
            self.assertEqual(report["s16"]["bytes"], 1600)
            self.assertEqual(report["ffmpeg_pcma"]["bytes"], 800)
            self.assertEqual(report["gst_compatible_pcma"]["bytes"], 800)
            for key in ("source", "s16", "ffmpeg_pcma", "gst_compatible_pcma"):
                self.assertRegex(report[key]["sha256"], r"^[0-9a-f]{64}$")
            for key in (
                "differing_bytes",
                "differing_percent",
                "decoded_error_rms",
                "decoded_error_dbfs",
                "signal_rms",
                "snr_db",
            ):
                self.assertIn(key, report)
            self.assertFalse(list(directory.glob(f".{output.name}.*.tmp")))


class CompareSnapshotTests(unittest.TestCase):
    def test_replaced_original_does_not_change_hashed_or_decoded_snapshot(self):
        snapshot = b"SNAPSHOT_SECRET_AUDIO"
        replacement = b"REPLACEMENT_AUDIO_DATA"
        snapshot_s16 = pack_s16([-1000, 1000])
        replacement_s16 = pack_s16([-2000, 2000])
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source.bin"
            source.write_bytes(snapshot)
            real_open = pathlib.Path.open
            original_read_count = 0
            decoded_paths = []

            class ReplaceAfterClose:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    return self.handle.__enter__()

                def __exit__(self, *exc_info):
                    result = self.handle.__exit__(*exc_info)
                    replacement_path = source.with_name("replacement.bin")
                    replacement_path.write_bytes(replacement)
                    os.replace(replacement_path, source)
                    return result

            def controlled_open(path, *args, **kwargs):
                nonlocal original_read_count
                mode = args[0] if args else kwargs.get("mode", "r")
                handle = real_open(path, *args, **kwargs)
                if path == source and mode == "rb":
                    original_read_count += 1
                    if original_read_count == 1:
                        return ReplaceAfterClose(handle)
                return handle

            def decode(path, sample_rate):
                decoded_path = pathlib.Path(path)
                decoded_paths.append(decoded_path)
                data = decoded_path.read_bytes()
                return snapshot_s16 if data == snapshot else replacement_s16

            with patch.object(pathlib.Path, "open", controlled_open), patch.object(
                backchannel_audio, "decode_source", side_effect=decode
            ), patch.object(
                backchannel_audio, "encode_pcma_ffmpeg", return_value=b"\xd5\xd5"
            ), patch.object(
                backchannel_audio,
                "encode_pcma_gst_compatible",
                return_value=b"\xd5\xd5",
            ):
                report = compare_pcma_encoders.compare(source, 0.05, 8000)

            rendered = json.dumps(report, sort_keys=True)
            self.assertEqual(original_read_count, 1)
            self.assertEqual(source.read_bytes(), replacement)
            self.assertEqual(report["file"], str(source))
            self.assertEqual(report["source"], {
                "bytes": len(snapshot),
                "sha256": hashlib.sha256(snapshot).hexdigest(),
            })
            self.assertEqual(report["s16"], {
                "bytes": len(snapshot_s16),
                "sha256": hashlib.sha256(snapshot_s16).hexdigest(),
            })
            self.assertEqual(len(decoded_paths), 1)
            self.assertNotEqual(decoded_paths[0], source)
            self.assertFalse(decoded_paths[0].exists())
            self.assertNotIn(str(decoded_paths[0]), rendered)
            self.assertNotIn("SNAPSHOT_SECRET_AUDIO", rendered)

    def test_snapshot_temp_file_is_removed_on_error_and_keyboard_interrupt(self):
        for error_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(error=error_type.__name__), tempfile.TemporaryDirectory() as directory:
                source = pathlib.Path(directory) / "source.bin"
                source.write_bytes(b"source snapshot")
                decoded_paths = []

                def fail_decode(path, sample_rate):
                    decoded_paths.append(pathlib.Path(path))
                    raise error_type("decode stopped")

                with patch.object(
                    backchannel_audio, "decode_source", side_effect=fail_decode
                ), self.assertRaises(error_type):
                    compare_pcma_encoders.compare(source, 0.05, 8000)

                self.assertEqual(len(decoded_paths), 1)
                self.assertNotEqual(decoded_paths[0], source)
                self.assertFalse(decoded_paths[0].exists())

    def test_snapshot_size_limit_is_enforced_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source.bin"
            source.write_bytes(b"1234")
            with patch.object(
                backchannel_audio, "MAX_SOURCE_FILE_BYTES", 3
            ), patch.object(backchannel_audio, "decode_source") as decode, \
                    self.assertRaisesRegex(ValueError, "source file.*exceeds"):
                compare_pcma_encoders.compare(source, 0.05, 8000)

            decode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
