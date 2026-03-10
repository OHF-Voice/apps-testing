# Speech-to-text Tester

[Wyoming][wyoming] speech-to-text ([STT][stt]) app used to test multiple STT services in Home Assistant.

This app looks like a regular STT service, but it:

1. Streams audio to the selected "primary" STT entity, returning that transcript
2. Send batched audio to selected additional STT entities
3. Saves the recorded audio and transcripts for all selected STT entities as a session

Users can review and delete sessions via the app's web UI.

[wyoming]: https://www.home-assistant.io/integrations/wyoming/
[stt]: https://www.home-assistant.io/integrations/stt/
