# Audio fixtures

Tier 3 `e2e_voice_pipeline` plays back recorded WAV files into GLaDOS to
verify the full STT → LLM → TTS chain. Tier 3's other test
(`stt_synth_roundtrip`) does NOT need fixtures — it generates its own
audio via TTS first.

This directory is intentionally empty in git. Recordings are
operator-environment-specific (your microphone, your room, your voice)
and shouldn't be checked in.

## Required fixtures

| Filename                  | Contents                          | Used by                         |
|---------------------------|-----------------------------------|---------------------------------|
| `query_what_time.wav`     | Saying "what time is it"          | `tier3::e2e_voice_pipeline`     |

`wake_word.wav` is **not** required — this container has no wake-word
detector. Wake-word handling lives upstream in Home Assistant. If you
see references to `wake_word.wav` in old issues/PRs, ignore them.

## Recording specs

- Format: WAV, mono, 16 kHz, 16-bit PCM
- Duration: 1-3 seconds
- Don't include leading silence beyond ~200 ms — STT works fine but
  longer silence inflates the test runtime
- Ambient room tone is fine; over-clean studio audio is not necessary

Quick way on Windows (PowerShell, requires `sox`):

```powershell
sox -d -r 16000 -c 1 -b 16 query_what_time.wav trim 0 3
```

Or record in Audacity, export as
`Microsoft WAV / Signed 16-bit PCM / 16000 Hz / Mono`.

## Updating expected transcripts

`expected_transcripts.json` lists the literal text each fixture should
transcribe to. Update it whenever you record a new fixture so future
contributors know what to compare against.

## Running Tier 3 with fixtures

```powershell
.\smoke.ps1 -Full
```

`-Full` flips Tier 3 on. The default Tier 3 test (`stt_synth_roundtrip`)
runs without fixtures; `e2e_voice_pipeline` only runs when fixtures
exist AND `--include-mutating` is set (it writes to the conversation
store). Run that one explicitly:

```bash
pytest tests/smoke -m tier3 --include-mutating
```

If a fixture is missing, the test skips with a clear message — it does
not fail.
