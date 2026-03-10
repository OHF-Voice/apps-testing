# Speech-to-text Tester

[Wyoming][wyoming] speech-to-text ([STT][stt]) app used to test multiple STT services in Home Assistant.

This app looks like a regular STT service, but it:

1. Streams audio to the selected "primary" STT entity, returning that transcript
2. Send batched audio to selected additional STT entities
3. Saves the recorded audio and transcripts for all selected STT entities as a session

Users can review and delete sessions via the app's web UI.

## Installation & Usage

Once the app is installed, an "stt-tester" Wyoming service should be
automatically discovered in "Settings -> Devices & services". Click "add" and
following the instructions.

Next, in "Settings -> Voice Assistants", select or create a new voice assistant
and choose "stt-tester" for the Speech-to-text part of the pipeline.

Finally, visit the "stt-tester" app in "Settings -> Apps" and click the "Open
Web UI" button to open the web UI. Click the "STT Settings" link and pick a
"primary" STT entity whose transcripts will be returned to the voice assistant.
Then choose which other STT entities you want to get transcriptions from.

Go back to sessions, and give the voice assistant a command. Refresh the page,
and you should see a new session. You can listen to the recorded audio, view the
transcripts for each STT entity, add notes to the session, or delete.

[wyoming]: https://www.home-assistant.io/integrations/wyoming/
[stt]: https://www.home-assistant.io/integrations/stt/
