# DCA contributor execution boundaries

Use the current checkout as the engineering root. Verify its canonical path,
Git top/branch/HEAD and dirty state before edits. For a source snapshot without
Git, identify the root through pyproject.toml and do not invent repository history.
Read the user's current task, docs/dispatcher_for_codex_agents.md (technical baseline),
docs/project_governance.md (rules), and docs/contract_v1.md (interface).
Do not import another project's scientific plans as DCA development rules.

Only change approved files. Use apply_patch for authored edits; preview and back
up overwrites/deletions. Preserve unrelated dirty state and private historical
artifacts. Do not commit, create remotes, push or tag without explicit authority.

Use the project's own .venv and scripts/dca-env.sh. Temporary files, logs and tests
stay in runtime; shared toolchains require explicit path configuration and write
authorization. Never install home bin entries without prior permission; managed
Python installation uses --no-bin. Do not silently use home or system tmp.

Tests and demos are fake/offline by default. No real model, external agent, phone request,
automatic retry/fallback, recursion or multi-writer operation without authorization.
Keep secrets outside Git/distributable files, configurations 0600 and private
runtime directories 0700. Never reset or replay a delivery/event ledger.

Use the current contract v1 names; retired project-specific names have no aliases.
Do not expand native subagent scheduling capabilities or
automatically adjudicate scientific results. Finish with scoped/full tests,
Ruff/Black/diff, artifact hashes and a boundary audit. Clearly distinguish fake
validation from real host validation; use no invented badges or license grants.

Default communication: 简体中文; keep paths, commands and schema names unchanged.
