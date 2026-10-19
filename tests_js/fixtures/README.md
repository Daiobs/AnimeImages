# Synthetic media fixture

`opus-tone.webm` is an original generated 440 Hz tone, not upstream media.
It contains only one Opus audio track (48 kHz, mono), 0.128 seconds, 959 bytes.
Local ffprobe verified the WebM container and absence of a video track.

Generation (FFmpeg/libopus; not required to run the Node tests):

```sh
ffmpeg -nostdin -v error -f lavfi -i sine=frequency=440:sample_rate=48000:duration=0.12 \
  -c:a libopus -b:a 16k -map_metadata -1 -f webm opus-tone.webm
```

The regression substitutes the HTTP response and save handoff. It does not
establish real upstream access, CORS, or a browser download to disk.
