# ONVIF RTSP Audio Backchannel

카메라의 ONVIF RTSP 백채널로 음원 파일을 한 번 전송하고 종료하는 Python 도구입니다.
GStreamer는 사용하지 않습니다. FFmpeg는 입력 파일을 mono 8kHz PCM으로 디코딩할 때만 사용하고,
PCMA(A-law) 인코딩과 RTP/RTSP 전송은 Python 코드가 처리합니다.

## 준비

- Python 3
- FFmpeg (`ffmpeg` 명령이 `PATH`에 있어야 함)

macOS에서는 다음 명령으로 FFmpeg를 설치할 수 있습니다.

```bash
brew install ffmpeg
```

## 음원 파일 한 번 재생

저장소 루트에서 실행합니다. MP3와 WAV 등 FFmpeg가 디코딩할 수 있는 파일을 사용할 수 있습니다.

```bash
python3 python/onvif_play.py \
  --host 10.128.10.141 \
  --user admin \
  --pass '<CAMERA_PASSWORD>' \
  --file '/absolute/path/to/audio.mp3'
```

음원 전송이 끝나면 RTSP 세션을 종료합니다. 이벤트마다 위 명령을 한 번 실행하면 됩니다.

현재 기본값은 SM-DM-4M2W에서 정상 재생을 확인한 다음 프로필입니다.

- PCMA(G.711 A-law), mono 8kHz
- TCP interleaved RTP
- 패킷 간격 40ms
- Python A-law 인코더 (`python-alaw`)
- rebase pacer, sender RTP identity, 첫 패킷 marker
- 볼륨 0.05, preroll 없음, RTCP sender report 없음

설정을 모두 명시하려면 다음 명령을 사용합니다.

```bash
python3 python/onvif_play.py \
  --host 10.128.10.141 \
  --user admin \
  --pass '<CAMERA_PASSWORD>' \
  --file '/absolute/path/to/audio.mp3' \
  --volume 0.05 \
  --encoder python-alaw \
  --codec pcma \
  --sample-rate 8000 \
  --transport tcp \
  --packet-ms 40 \
  --pacer rebase \
  --rtp-identity sender \
  --marker-mode first \
  --preroll-ms 0 \
  --rtcp-interval 0
```

## 테스트

```bash
PYTHONPATH=python:. python3 -m unittest discover -s python -p 'test_*.py'
```
