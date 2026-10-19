kind: Fixed
task: t_8fccf0cd

- **`hscc-project/README.md` + `docs/README.md` rewritten to the post-F1
  branding state** (audit `README_REVIEW_2026-10-15.md` R3, docs-only). The
  front page still quoted the pre-F1 `init` transcript (`flightdeck home:`)
  and titled itself after the old tool name 16 times; it now quotes the
  render captured live off main (`hscc-project home: … (dry run — use --apply
  to write)`), re-titles to "hscc project — project/kanban control", and cuts
  the 2026-08-09 anecdote to 3 lines pointing at `docs/INCIDENTS.md`. 16
  `flightdeck` mentions -> 7, and the survivors are the technical-necessity
  ones only: the `~/.flightdeck/*` on-disk paths (`init.py:64` `DEFAULT_HOME`)
  and the packaged console-script identifier in the MCP registration snippet
  (`pyproject.toml` `[project.scripts]`), each with a one-line explanation
  rather than a silent keep. `docs/assets/README.md` (a 6-liner about one
  image, "FlightDeck banner") is folded into `docs/README.md` as an `assets/`
  section and deleted; `banner.png` stays at `docs/assets/banner.png`.
- **Audit arithmetic corrected: the MCP server has 24 tools, not 25.** The R3
  row reads "`:287` \"24 tools\" — code has 25", and §2.5 repeats "MCP
  section: 25 tools". Verified against `hscc-project/flightdeck/mcp_server.py`
  on main: exactly 24 `@mcp.tool()` decorator sites (`:142`…`:458`) each
  binding one `def`, and no other registration path anywhere in the repo
  (`.tool(` appears nowhere else). The README's existing "24 tools" was
  already right, so the row is kept at 24 and the tool list was corrected to
  match the code (it double-listed `reconcile` and omitted `reconcile
  preview`). `docs/APPROACH.md:118` ("25 tools") carries the same wrong number
  and is untouched here — it is not an R3 file.
