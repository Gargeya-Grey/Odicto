# Personal vocabulary and Polish

Open Setup, then Speech. Personal vocabulary is visible for every engine. Enter exact spellings separated by commas or lines. Gemini uses at most 100 normalized terms; Groq and local Whisper receive whole terms within 224 UTF-8 bytes. OpenRouter speech has no vocabulary hints. The legacy configuration key remains GEMINI_TRANSCRIBE_VOCABULARY.

Open Polish in the sidebar. Click Change and press the selection shortcut (default F8), or Disable to clear it.
The recorder and keycap display match the other shortcuts. Select a separate model;
the provider and saved key come from AI answers. The model picker is searchable, shows
the provider, and supports Use AI answers model or a separately labeled Custom model ID.
Check saved custom IDs survive navigation and catalog refresh, keyboard search/Enter/Esc
work, and searching alone does not mark settings changed. No automatic polish runs after dictation.
Only the owner saves Setup and restarts because Save restarts their running instance.

Owner journey: in Notepad select text containing a grammatical error and deliberate
emphasis. Press F8. Confirm the selection is replaced in place with minimal edits that
preserve meaning, emotion and paragraphs, the HUD says "Polished", and Ctrl+Z restores
the original. Confirm the clipboard holds what it held before F8. Repeat with no
selection, Esc during processing, and a changed selection: no result should be inserted. Confirm raw and F7 dictation never run polish.
Check behavior in the actual editor too: copy-based probes cannot prove field identity,
and some editors copy a line when nothing is selected. Terminal targets are refused.
Real recognition, editor selection behavior and editorial quality need owner confirmation.

Hook-free regression: python -m unittest tests.test_personal_vocabulary tests.test_selection_polish tests.test_polish_result tests.test_polish_upgrade tests.test_provider_transport. Full gate: tools/verify.ps1. Synthetic request tests prove routing and instructions, not provider compliance or microphone recognition.
