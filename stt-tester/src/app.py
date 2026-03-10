#!/usr/bin/env python3

import argparse
import asyncio
import logging
import time
import re
import json
import os
import shutil
import wave
import threading
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import List, Optional, Set, Dict, Union, Tuple

import aiohttp
from flask import Flask, render_template, jsonify, send_file, redirect, url_for, request
from werkzeug.middleware.proxy_fix import ProxyFix
from wyoming.asr import Transcript, Transcribe
from wyoming.audio import AudioChunk, AudioStop, AudioStart
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

_LOGGER = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"
SETTINGS_FILE = "settings.json"
BASE_DIR = Path(__file__).resolve().parent


@dataclass
class STTEntity:
    entity_id: str
    supported_languages: Set[str]

    _supported_lang_map: Dict[Tuple[str, Union[str, None]], str] = field(
        default_factory=dict
    )
    _language_map: Dict[str, str] = field(default_factory=dict)

    def get_best_language(self, language: str) -> Optional[str]:
        language = language.strip()

        if language in self.supported_languages:
            return language

        best_language = self._language_map.get(language)
        if best_language is not None:
            return best_language

        if not self._supported_lang_map:
            # {(family, region): language}
            for supported_lang in self.supported_languages:
                supported_lang_parts = re.split(r"[-_]", supported_lang)
                supported_lang_family = supported_lang_parts[0].lower()
                supported_lang_region = (
                    supported_lang_parts[1].upper()
                    if len(supported_lang_parts) > 1
                    else None
                )

                self._supported_lang_map[
                    (supported_lang_family, supported_lang_region)
                ] = supported_lang

        language_parts = re.split(r"[-_]", language)
        lang_family = language_parts[0].lower()
        lang_region = language_parts[1].upper() if len(language_parts) > 1 else None

        # Exact match
        best_language = self._supported_lang_map.get((lang_family, lang_region))

        if best_language is None:
            # Special cases
            if (lang_family == "en") and (lang_region is None):
                best_language = self._supported_lang_map.get(("en", "US"))

        if best_language is None:
            # Family only
            best_language = self._supported_lang_map.get((lang_family, None))

        if best_language is not None:
            self._language_map[language] = best_language
            return best_language

        return None


# -----------------------------------------------------------------------------


@dataclass
class State:
    hass_http_uri: str
    hass_token: str
    primary_entity_id: str
    additional_entities: set[str]

    async def get_entities(self) -> Dict[str, STTEntity]:
        entities: Dict[str, STTEntity] = {}
        headers = {"Authorization": f"Bearer {self.hass_token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.hass_http_uri}/states", headers=headers
            ) as response:
                states = await response.json()

            for state in states:
                entity_id = state.get("entity_id", "")
                if not entity_id.startswith("stt."):
                    continue

                if entity_id.startswith("stt.stt_tester"):
                    # Don't send test audio back to the tester itself (infinite loop).
                    continue

                _LOGGER.debug("Getting info for STT entity: %s", entity_id)

                async with session.get(
                    f"{self.hass_http_uri}/stt/{entity_id}", headers=headers
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning(
                            "Failed to get entity info: %s, status=%s",
                            entity_id,
                            resp.status,
                        )
                        continue

                    info = await resp.json()

                    # Check required audio format
                    if (
                        (16000 not in info["sample_rates"])
                        or (16 not in info["bit_rates"])
                        or (1 not in info["channels"])
                        or ("wav" not in info["formats"])
                        or ("pcm" not in info["codecs"])
                    ):
                        _LOGGER.warning(
                            "Skipping '%s': 16Khz 16-bit mono PCM is not supported",
                            entity_id,
                        )
                        _LOGGER.warning("%s: %s", entity_id, info)
                        continue

                    entities[entity_id] = STTEntity(
                        entity_id=entity_id,
                        supported_languages=set(info["languages"]),
                    )

        return entities


def load_settings(output_dir):
    settings_path = os.path.join(output_dir, SETTINGS_FILE)
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_settings(output_dir, settings):
    settings_path = os.path.join(output_dir, SETTINGS_FILE)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


class IngressPrefixMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        ingress_path = environ.get("HTTP_X_INGRESS_PATH", "")
        if ingress_path:
            environ["SCRIPT_NAME"] = ingress_path
            path_info = environ.get("PATH_INFO", "")
            if path_info.startswith(ingress_path):
                environ["PATH_INFO"] = path_info[len(ingress_path) :] or "/"
        return self.app(environ, start_response)


def get_app(state: State, args: argparse.Namespace) -> Flask:
    flask_app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_proto=1, x_host=1)
    flask_app.wsgi_app = IngressPrefixMiddleware(flask_app.wsgi_app)

    @flask_app.route("/")
    def index():
        return redirect(url_for("list_sessions"))

    @flask_app.route("/sessions")
    def list_sessions():
        output_dir = args.output_dir
        sessions = []

        if os.path.exists(output_dir):
            for item in sorted(os.listdir(output_dir), reverse=True):
                session_path = os.path.join(output_dir, item)
                if os.path.isdir(session_path):
                    audio_path = os.path.join(session_path, "audio.wav")
                    transcripts_path = os.path.join(session_path, "transcripts.json")

                    audio_exists = os.path.exists(audio_path)
                    transcripts = {}
                    if os.path.exists(transcripts_path):
                        with open(transcripts_path, "r", encoding="utf-8") as f:
                            transcripts = json.load(f)

                    notes_path = os.path.join(session_path, "notes.txt")
                    notes = ""
                    if os.path.exists(notes_path):
                        with open(notes_path, "r", encoding="utf-8") as f:
                            notes = f.read()

                    sessions.append(
                        {
                            "id": item,
                            "audio_exists": audio_exists,
                            "transcripts": transcripts,
                            "notes": notes,
                        }
                    )

        return render_template("sessions.html", sessions=sessions)

    @flask_app.route("/session/<session_id>/audio")
    def play_audio(session_id):
        output_dir = args.output_dir
        audio_path = os.path.join(output_dir, session_id, "audio.wav")

        if os.path.exists(audio_path):
            return send_file(audio_path, mimetype="audio/wav")
        return jsonify({"error": "Audio file not found"}), 404

    @flask_app.route("/session/<session_id>/delete", methods=["POST"])
    def delete_session(session_id):
        output_dir = args.output_dir
        session_path = os.path.join(output_dir, session_id)

        if os.path.exists(session_path):

            shutil.rmtree(session_path)
            return jsonify({"success": True})

        return jsonify({"error": "Session not found"}), 404

    @flask_app.route("/session/<session_id>/notes", methods=["POST"])
    def save_notes(session_id):
        output_dir = args.output_dir
        session_path = os.path.join(output_dir, session_id)
        notes_path = os.path.join(session_path, "notes.txt")

        data = request.get_json(silent=True)
        if data:
            notes = data.get("notes", "")
        else:
            notes = request.form.get("notes", "")

        with open(notes_path, "w", encoding="utf-8") as f:
            f.write(notes)

        return jsonify({"success": True})

    @flask_app.route("/settings")
    def settings_page():
        output_dir = args.output_dir
        saved_settings = load_settings(output_dir)
        return render_template(
            "settings.html",
            settings=saved_settings or {},
            primary_entity_id=state.primary_entity_id,
            additional_entities=list(state.additional_entities),
        )

    @flask_app.route("/settings/save", methods=["POST"])
    def save_settings_route():
        output_dir = args.output_dir
        data = request.get_json()

        new_settings = {
            "primary_entity_id": data.get("primary_entity_id", ""),
            "additional_entities": data.get("additional_entities", []),
        }

        state.primary_entity_id = new_settings["primary_entity_id"]
        state.additional_entities = set(new_settings["additional_entities"])

        save_settings(output_dir, new_settings)
        return jsonify({"success": True})

    @flask_app.route("/api/entities")
    def list_available_entities():
        loop = asyncio.new_event_loop()
        try:
            entities = loop.run_until_complete(state.get_entities())
            return jsonify({"entities": sorted(entities.keys())})
        finally:
            loop.close()

    return flask_app


# -----------------------------------------------------------------------------


async def main() -> None:
    """Runs fallback ASR server."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True, help="unix:// or tcp://")
    parser.add_argument(
        "--hass-token", required=True, help="Long-lived access token for Home Assistant"
    )
    parser.add_argument(
        "--hass-http-uri",
        default="http://homeassistant.local:8123/api",
        help="URI of Home Assistant HTTP API",
    )
    parser.add_argument(
        "--primary-entity-id",
        required=True,
        help="Entity id of the first STT system checked, and whose transcript is returned",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Output directory for saving audio and transcripts",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print DEBUG messages to console"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    _LOGGER.debug(args)

    # Create output directory if it doesn't exist
    os.makedirs(args.output_dir, exist_ok=True)

    state = State(
        hass_http_uri=args.hass_http_uri,
        hass_token=args.hass_token,
        primary_entity_id=args.primary_entity_id,
        additional_entities=set(),
    )

    # Load saved settings
    saved_settings = load_settings(args.output_dir)
    if saved_settings:
        if saved_settings.get("primary_entity_id"):
            state.primary_entity_id = saved_settings["primary_entity_id"]

        if saved_settings.get("additional_entities"):
            state.additional_entities = set(saved_settings["additional_entities"])

    # Create output directory if it doesn't exist
    os.makedirs(args.output_dir, exist_ok=True)

    server = AsyncServer.from_uri(args.uri)
    _LOGGER.info("Ready")

    flask_app = get_app(state, args)

    @flask_app.context_processor
    def inject_url_for():
        return dict(url_for=url_for)

    def run_flask():
        flask_app.run(host="0.0.0.0", port=5000, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    try:
        await server.run(
            partial(
                TestEventHandler,
                args.hass_token,
                args.hass_http_uri,
                args.output_dir,
                state,
            )
        )
    except KeyboardInterrupt:
        pass


# -----------------------------------------------------------------------------


class TestEventHandler(AsyncEventHandler):
    """Event handler for clients."""

    def __init__(
        self,
        hass_token: str,
        hass_http_uri: str,
        output_dir: str,
        state: State,
        *args,
        **kwargs,
    ) -> None:
        """Initialize event handler."""
        super().__init__(*args, **kwargs)

        self.hass_token = hass_token
        self.hass_http_uri = hass_http_uri
        self.output_dir = output_dir
        self.state = state
        self.client_id = str(time.monotonic_ns())

        self._language = DEFAULT_LANGUAGE

        self._audio_queue: asyncio.Queue[Union[bytes, None]] = asyncio.Queue()
        self._primary_write_task: Optional[asyncio.Task] = None
        self._saved_audio_chunks: List[bytes] = []
        self._session: Optional[aiohttp.ClientSession] = None

        # entity_id -> {"text": str, "target_language": str}
        self._transcripts: Dict[str, Dict[str, str]] = {}

        # Session notes
        self._notes: str = ""

        # Session directory for this client
        self._session_dir: Optional[str] = None

        self._info_event: Optional[Event] = None

        self.entities: Dict[str, STTEntity] = {}

    async def handle_event(self, event: Event) -> bool:
        """Handle Wyoming event."""
        try:
            return await self._handle_event(event)
        except Exception:
            _LOGGER.exception("Error handling event")

        return True

    async def _handle_event(self, event: Event) -> bool:
        """Handle Wyoming event."""
        if Describe.is_type(event.type):
            await self._write_info()
            return True

        if AudioStart.is_type(event.type):
            self.entities = await self.state.get_entities()
        elif AudioChunk.is_type(event.type):
            if self._primary_write_task is None:
                # Always start with primary entity
                self._primary_write_task = asyncio.create_task(
                    self._write_audio(self.state.primary_entity_id, self.entities)
                )

            chunk = AudioChunk.from_event(event)
            self._audio_queue.put_nowait(chunk.audio)
            self._saved_audio_chunks.append(chunk.audio)
        elif AudioStop.is_type(event.type):
            self._audio_queue.put_nowait(None)
            if self._primary_write_task is not None:
                await self._primary_write_task

            # Create session directory
            self._session_dir = os.path.join(self.output_dir, self.client_id)
            os.makedirs(self._session_dir, exist_ok=True)

            try:
                # Get transcripts for other STT entities
                for entity_id in self.entities:
                    if (entity_id == self.state.primary_entity_id) or (
                        entity_id not in self.state.additional_entities
                    ):
                        continue

                    _LOGGER.debug(entity_id)
                    self._audio_queue = asyncio.Queue()
                    for chunk in self._saved_audio_chunks:
                        self._audio_queue.put_nowait(chunk)
                    self._audio_queue.put_nowait(None)

                    await self._write_audio(entity_id, self.entities)
            finally:
                # Save audio and transcripts
                if self._saved_audio_chunks:
                    audio_path = os.path.join(self._session_dir, "audio.wav")
                    with wave.open(audio_path, "wb") as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(16000)
                        for chunk in self._saved_audio_chunks:
                            wav_file.writeframes(chunk)

                transcripts_path = os.path.join(self._session_dir, "transcripts.json")
                with open(transcripts_path, "w", encoding="utf-8") as f:
                    json.dump(self._transcripts, f, indent=2)

                notes_path = os.path.join(self._session_dir, "notes.txt")
                with open(notes_path, "w", encoding="utf-8") as f:
                    f.write(self._notes)

                # Reset
                self._primary_write_task = None
                self._language = DEFAULT_LANGUAGE
                self._saved_audio_chunks = []
                self._audio_queue = asyncio.Queue()
                self._notes = ""

                if self._session is not None:
                    await self._session.close()
                    self._session = None
        elif Transcribe.is_type(event.type):
            transcribe = Transcribe.from_event(event)
            self._language = transcribe.language or DEFAULT_LANGUAGE
            self._transcripts.clear()

        return True

    async def _write_audio(
        self, entity_id: str, entities: Dict[str, STTEntity]
    ) -> None:
        if entity_id not in entities:
            _LOGGER.warning("Entity no longer available: %s", entity_id)
            return

        target_entity = entities[entity_id]
        target_language = target_entity.get_best_language(self._language)
        if target_language is None:
            _LOGGER.warning(
                "STT entity does not supported language: %s", self._language
            )
            target_language = self._language

        _LOGGER.debug(
            "Trying entity %s with language %s",
            target_entity.entity_id,
            target_language,
        )

        headers = {
            "Authorization": f"Bearer {self.hass_token}",
            "Content-Type": "audio/wav",
            "X-Speech-Content": f"language={target_language};format=wav;codec=pcm;bit_rate=16;sample_rate=16000;channel=1",
        }

        async def audio_stream():
            while True:
                chunk = await self._audio_queue.get()
                if chunk is None:
                    break

                yield chunk

        transcript = ""

        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()

            async with self._session.post(
                f"{self.hass_http_uri}/stt/{target_entity.entity_id}",
                headers=headers,
                data=audio_stream(),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    _LOGGER.debug("Result from %s: %s", target_entity.entity_id, result)
                    transcript = result.get("text", "").strip()
        except Exception:
            _LOGGER.exception("Error writing audio")

        self._transcripts[entity_id] = {
            "text": transcript,
            "target_language": target_language,
        }
        if entity_id == self.state.primary_entity_id:
            # Always return primary transcript
            await self.write_event(Transcript(text=transcript).event())

    async def _write_info(self) -> None:
        if self._info_event is not None:
            await self.write_event(self._info_event)
            return

        supported_languages: Set[str] = set()
        entities = await self.state.get_entities()
        for entity in entities.values():
            supported_languages.update(entity.supported_languages)

        info = Info(
            asr=[
                AsrProgram(
                    name="stt-tester",
                    attribution=Attribution(
                        name="The Home Assistant Authors",
                        url="http://github.com/OHF-voice",
                    ),
                    description="Tests multiple speech-to-text systems",
                    installed=True,
                    version="0.0.1",
                    models=[
                        AsrModel(
                            name="stt-tester",
                            attribution=Attribution(
                                name="The Home Assistant Authors",
                                url="http://github.com/OHF-voice",
                            ),
                            installed=True,
                            description="Multiple STT systems",
                            version=None,
                            languages=list(supported_languages),
                        )
                    ],
                )
            ]
        )

        self._info_event = info.event()
        await self.write_event(self._info_event)


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
