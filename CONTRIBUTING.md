# Contributing to Devi

Thank you for considering a contribution to Devi. This guide explains how to prepare changes, run the bot locally, validate your work, and submit a pull request.

Before making changes, read the [README](README.md) to understand the bot's features and architecture, and read [LICENSE](LICENSE.md) to understand the terms that apply to the project.

## 1. Contribution scope

This repository contains the Devi Discord bot. Its main code is in `Devi/`. The web dashboard has a separate repository: [security-camera/Devi-Site](https://github.com/security-camera/Devi-Site). Changes to the dashboard UI should be proposed there; changes to the bot's dashboard API belong here.

You can contribute in several ways:

- Fix bugs or improve existing commands and services.
- Propose and implement features.
- Improve documentation and examples.
- Add or improve translations.
- Improve reliability, performance, accessibility, or security.

For substantial changes, open an issue first and describe the problem and proposed approach. This helps avoid duplicate work and makes it possible to discuss compatibility before implementation. Small documentation fixes and obvious bug fixes can be submitted directly.

## 2. License and contribution rights

Devi is distributed under the **Devi Non-Commercial License (DNCL) 2.0**. It is a source-available license; do not assume that the project is licensed under a standard permissive open-source license.

Section 14 of [LICENSE](LICENSE.md) applies to contributions:

- Contributions to the official project must be capable of being distributed under DNCL 2.0 and any other license selected by the Copyright Holder for the project, unless otherwise agreed in writing.
- By submitting a contribution, you confirm that you have the necessary rights to submit it and authorize its inclusion in the project.
- Submitting a contribution does not, by itself, transfer ownership of that contribution to the Copyright Holder.

Only submit code, translations, documentation, fonts, images, and other materials that you created or are authorized to contribute under terms compatible with the project. Preserve applicable third-party notices and licenses.

Read the full license before submitting changes. In particular, DNCL 2.0 restricts commercial forks and certain commercial uses of derivative works. Do not treat permission to use the original Devi software as permission to create or distribute a commercial derivative.

## 3. Set up a development environment

### Requirements

- Python 3.10 or newer. The codebase uses modern Python syntax, including union type operators.
- PostgreSQL, reachable through `DATABASE_URL`.
- A Discord bot application and a separate test server for testing Discord commands.
- FFmpeg in `PATH` for music playback and media operations that use FFmpeg. Video captioning also uses `ffprobe`.

Piper voice models are required only for the TTS voices you intend to test. They are not included in the repository.

### Clone and install dependencies

From the repository root:

```bash
git clone https://github.com/security-camera/Devi-Discord-Bot.git
cd Devi-Discord-Bot/Devi

python -m venv .venv
```

Activate the virtual environment and install the dependencies:

**Windows PowerShell**

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

**Linux / macOS**

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Install FFmpeg separately using the instructions for your operating system, and make sure both `ffmpeg` and, where needed, `ffprobe` can be found through `PATH`.

### Configure the bot

The application reads its environment file from `Devi/config/.env`. Create it from the example:

**Windows PowerShell**

```powershell
Copy-Item config/env_example.env config/.env
```

**Linux / macOS**

```bash
cp config/env_example.env config/.env
```

Edit `config/.env` and provide the values needed by your local instance:

- `BOT_TOKEN` — token for your development bot.
- `TECHNICAL_SUPPORT_SERVER` and `AI_SIGNAL_CHANNEL` — Discord IDs used by the bot.
- `DATABASE_URL` — connection string for your development PostgreSQL database.
- `ENCRYPTION_KEY` — a Fernet key used by the encryption helpers.

Generate an encryption key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the key safe. Data encrypted with it cannot be decrypted if the key is lost. Never reuse production credentials in a development environment.

For testing, set `TEST_ENABLED=1` and provide `BOT_TOKEN_TEST`, `TECHNICAL_SUPPORT_SERVER_TEST`, and `AI_SIGNAL_CHANNEL_TEST`. Set `TEST_SERVER` to your test guild ID if you need guild-scoped application commands to register while developing. Use a separate test bot, test guild, and database; do not experiment against production data.

The database schema is initialized by the bot on startup. The AI provider, Top.gg integration, and dashboard API have additional optional settings documented in [README](README.md) and `config/env_example.env`. You do not need to configure external AI services to work on unrelated parts of the bot.

To start the bot, run this command from the `Devi/` directory:

```bash
python main.py
```

### Optional TTS voices

Piper voice model files are not stored in Git. To test TTS, download both the `.onnx` model and its matching `.onnx.json` configuration file from the [Piper voices collection](https://huggingface.co/rhasspy/piper-voices) and place them in `Devi/config/voices/`. See [Devi/config/voices/README.md](Devi/config/voices/README.md) for the voice links used by the project.

## 4. Project layout

The most relevant locations are:

| Path                                      | Purpose                                                         |
|-------------------------------------------|-----------------------------------------------------------------|
| `Devi/main.py`                            | Bot entry point, initialization, and extension loading          |
| `Devi/cogs/`                              | Discord commands, listeners, and feature modules                |
| `Devi/apis_integration/`                  | Integrations with external APIs                                 |
| `Devi/config/locales/`                    | Translation files                                               |
| `Devi/config/fonts/`                      | Fonts used by image rendering                                   |
| `Devi/config/voices/`                     | Local Piper voice models; not committed                         |
| `Devi/db.py`                              | PostgreSQL connection pool and database schema                  |
| `Devi/permissions.py`                     | Custom permission system                                        |
| `Devi/i18n.py` and `Devi/discord_i18n.py` | Translation lookup and Discord localization helpers             |
| `Devi/crypto_utils.py`                    | Encryption and decryption helpers                               |
| `Devi/paths.py`                           | Shared project and configuration paths                          |

## 5. Code and implementation guidelines

Please follow the conventions already used in the surrounding module. The repository does not currently define a mandatory formatter or linter configuration, so avoid introducing unrelated formatting changes.

### Python and Discord code

- Use four spaces for indentation and keep names consistent with the existing code: `snake_case` for functions and variables, `PascalCase` for classes, and uppercase names for constants.
- Keep feature-specific commands and event handlers in the appropriate Cog. Put reusable functionality in shared modules rather than duplicating it across Cogs.
- Follow disnake's existing command, interaction, and extension patterns.
- Keep Discord event handlers asynchronous. Do not perform slow network, file, or media work on the event loop; use the existing asynchronous APIs or appropriate helpers such as `asyncio.to_thread` and executors.
- Limit expensive parallel work where appropriate. Follow existing semaphore and timeout patterns for media processing and external requests.
- Use the shared path helpers in `paths.py` instead of hardcoding project paths.
- Keep changes focused. Do not reformat or rename unrelated code as part of a bug fix.

### Database and data handling

- Use the shared database helpers in `db.py` rather than creating ad hoc connections for routine queries.
- Pass SQL parameters separately using psycopg placeholders (`%s`). Do not interpolate user input into SQL strings.
- Use `db_cursor(commit=True)` for transactions that write data; read-only operations should not commit.
- Keep database operations short because the current database helpers are synchronous.
- If you change the database schema, review `SCHEMA` in `db.py` and provide safe upgrade handling for existing installations. The current project does not use a separate versioned migration framework; adding a column to a `CREATE TABLE IF NOT EXISTS` statement alone does not update an already existing table.
- Use `crypto_utils.encrypt()` and `crypto_utils.decrypt()` where sensitive text must be encrypted before storage. Never log API keys, bot tokens, encryption keys, or private user data.

### Localization

All user-facing bot text should use the localization system instead of hardcoded messages. Use `i18n.t()` for runtime text and `localized()` from `discord_i18n.py` for Discord command metadata and choices.

Translations are stored as separate JSON files in `Devi/config/locales/`. The current files are `bg.json`, `de.json`, `en-US.json`, `es-419.json`, `fi.json`, `ru.json`, and `uk.json`.

When adding or changing text:

1. Add the key to the default `en-US.json` file.
2. Keep the same nested key structure in the other locale files.
3. Preserve interpolation placeholders exactly (for example, `{member}` or `{count}`).
4. Preserve the `_meta` object and any custom emoji placeholders.
5. Use valid JSON and UTF-8 encoding. Follow the existing four-space indentation.

The localization module also defines aliases (currently including `es-ES` to `es-419`); an alias does not require a separate locale file unless the implementation is intentionally changed.

### Configuration and repository hygiene

- Add new environment variables to `Devi/config/env_example.env`, with a safe default or a clear explanation where appropriate.
- Never commit `Devi/config/.env`, real tokens, API keys, database credentials, private data, local voice models, or generated runtime files.
- Do not add large binary assets unless they are necessary, authorized for inclusion, and their licensing is clear.
- Keep third-party code and assets under their original licenses and retain required attribution.

## 6. Validation and testing

At the time this guide was written, the repository did not contain a checked-in automated test suite, test-runner configuration, or CI workflow. Do not assume that `pytest`, Ruff, or Black is configured.

Run the following checks from the `Devi/` directory before submitting:

**Compile all Python modules**

```bash
python -m compileall -q .
```

**Validate the locale JSON files**

```bash
python -c "import json, pathlib; files = list(pathlib.Path('config/locales').glob('*.json')); [json.loads(p.read_text(encoding='utf-8')) for p in files]; print(f'Validated {len(files)} locale files')"
```

Also perform a manual smoke test relevant to your change:

- Start the bot with a separate development bot token and database.
- Confirm that the changed Cog loads and the affected command or event works.
- Check permissions, error handling, and behavior in both a guild and a DM where relevant.
- For database changes, test against both a fresh database and a database containing the previous schema.
- For localization changes, verify the affected text in each locale you changed.
- For media, voice, or API changes, test the relevant external dependency and failure paths where practical.

In your pull request, list the checks you actually ran. If a test could not be run because an external service, token, model, or other dependency was unavailable, say so explicitly.

## 7. Commits and pull requests

Create a focused branch from `master`. The following branch prefixes are recommended:

- `feat/` — new functionality.
- `fix/` — bug fixes.
- `docs/` — documentation changes.
- `i18n/` — localization changes.
- `refactor/` — internal restructuring without intended behavior changes.

Use a short, descriptive commit message. Keep each pull request focused on one logical change and avoid mixing unrelated refactoring with feature work.

A pull request should include:

- A clear description of the problem and the proposed solution.
- Links to related issues, if any.
- A summary of behavior changes, including configuration or database changes.
- The validation steps you performed and their results.
- Screenshots or short recordings when they help explain a user-facing UI or media change.

Before opening the pull request, check that secrets, local configuration, generated files, AI instructions (`CLAUDE.md` or `AGENTS.md`) and downloaded voice models are not included in the diff. Be prepared to revise the contribution in response to review feedback.

## 8. Reporting security issues

Do not publish credentials, tokens, private user data, or exploitable vulnerability details in a public issue or pull request. For a suspected security vulnerability, contact the maintainer privately through GitHub rather than disclosing it publicly.

---

By contributing, you confirm that you have read and will comply with the applicable terms in [LICENSE](LICENSE.md).
