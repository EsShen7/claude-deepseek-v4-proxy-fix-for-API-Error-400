# Claude Code DeepSeek V4 Proxy Fix for API Error 400

A small local compatibility proxy for using DeepSeek V4 models with Claude Code when tool calls or long conversations fail with:

```text
API Error: 400 The `content[].thinking` in the thinking mode must be passed back to the API.
```

## Why This Exists

DeepSeek V4 enables thinking/reasoning behavior by default. In multi-turn agent workflows, especially after tool calls or context compaction, DeepSeek expects the previous assistant reasoning content to be passed back correctly.

Claude Code can talk to DeepSeek through the Anthropic-compatible endpoint:

```text
https://api.deepseek.com/anthropic
```

However, this route can fail in tool-call loops because the thinking/reasoning fields are not round-tripped in the shape DeepSeek V4 expects.

This proxy avoids that failing path:

```text
Claude Code -> local Anthropic-compatible proxy -> DeepSeek chat/completions
```

The proxy accepts Claude Code's Anthropic Messages-style requests locally, converts them to DeepSeek's OpenAI-compatible `chat/completions` format, and sends:

```json
{
  "thinking": {
    "type": "disabled"
  }
}
```

That keeps Claude Code tool calls usable and avoids the `content[].thinking` 400 error.

## Files

- `deepseek_anthropic_proxy.py` - the local proxy server.
- `start-deepseek-proxy.ps1` - Windows PowerShell helper for starting the proxy in the background.
- `claude-settings.example.json` - example Claude Code settings.

## Requirements

- Python 3.10 or newer.
- Claude Code.
- A valid DeepSeek API key.
- Windows PowerShell if you use `start-deepseek-proxy.ps1`.

No third-party Python packages are required.

## Start the Proxy

From this repository:

```powershell
.\start-deepseek-proxy.ps1
```

If Python is not on `PATH`, pass the full Python path:

```powershell
.\start-deepseek-proxy.ps1 -PythonPath C:\path\to\python.exe
```

The proxy listens on:

```text
http://127.0.0.1:8765/anthropic
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

Expected response:

```json
{
  "ok": true
}
```

## Configure Claude Code

Edit your Claude Code settings file, usually:

```text
%USERPROFILE%\.claude\settings.json
```

Set the Anthropic base URL to the local proxy:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8765/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "YOUR_DEEPSEEK_API_KEY",
    "ANTHROPIC_MODEL": "DeepSeek-V4-Flash",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "DeepSeek-V4-Flash",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "DeepSeek-V4-Flash",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "DeepSeek-V4-Flash"
  }
}
```

Keep your real API key only in your local settings. Do not commit it.

## Verify

Run a simple Claude Code request:

```powershell
claude -p "Reply with OK only." --tools "" --no-session-persistence
```

Then verify a tool-call loop:

```powershell
claude -p "Use the Bash tool to run exactly: echo hello. Then reply with exactly TOOL_OK." --no-session-persistence
```

Expected response:

```text
TOOL_OK
```

## Notes and Limitations

- This proxy disables DeepSeek V4 thinking mode for stability.
- Disabling thinking can reduce deep reasoning quality for complex tasks, but normal coding and tool-call workflows remain usable.
- This is a lightweight compatibility shim, not a full production gateway like Hermes.
- The proxy currently focuses on Claude Code text and tool-call flows.
- Do not expose this proxy publicly. It is intended for `127.0.0.1` only.

## Troubleshooting

If Claude Code still fails:

1. Make sure the proxy is running.
2. Make sure `ANTHROPIC_BASE_URL` points to `http://127.0.0.1:8765/anthropic`.
3. Start a new Claude Code session instead of continuing an old one with `--continue`.
4. Check `deepseek-proxy.err.log` for upstream API errors.

If the error changes to a model access error, confirm that your DeepSeek account can access the configured model, such as `DeepSeek-V4-Flash` or `DeepSeek-V4-Pro`.
