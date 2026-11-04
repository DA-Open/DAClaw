# Optional Skills

Official skills maintained by DA-Open that are **not activated by default**.

These skills ship with the daclaw-agent repository but are not copied to
`~/.daclaw/skills/` during setup. They are discoverable via the Skills Hub:

```bash
daclaw skills browse               # browse all skills, official shown first
daclaw skills browse --source official  # browse only official optional skills
daclaw skills search <query>       # finds optional skills labeled "official"
daclaw skills install <identifier> # copies to ~/.daclaw/skills/ and activates
```

## Why optional?

Some skills are useful but not broadly needed by every user:

- **Niche integrations** — specific paid services, specialized tools
- **Experimental features** — promising but not yet proven
- **Heavyweight dependencies** — require significant setup (API keys, installs)

By keeping them optional, we keep the default skill set lean while still
providing curated, tested, official skills for users who want them.
