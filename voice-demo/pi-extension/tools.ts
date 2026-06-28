// Pi extension for voice-demo: custom coding-assistant tools.
//
// Replaces pi's builtin read/grep/find/ls/write with parameterized,
// injection-safe equivalents. write_file is restricted to docs/patchbay/
// at the tool layer — not just the system prompt.
//
// All exec calls use argv arrays (never shell strings) so metacharacters
// like &&, ;, |, $() are inert — passed as literal argument bytes.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"
import { Type } from "typebox"
import * as path from "node:path"
import * as fs from "node:fs/promises"

// Allowlist for git refs: hashes, branch names, tags, colon for ref:path in git_show_file.
const REF_RE = /^[a-zA-Z0-9_./:@^~-]+$/

function ok(text: string) {
  return { content: [{ type: "text" as const, text }], details: undefined }
}

function safeProjectPath(cwd: string, rel: string): string {
  if (path.isAbsolute(rel)) throw new Error(`Absolute paths not allowed: ${rel}`)
  const resolved = path.resolve(cwd, rel)
  const base = cwd.endsWith(path.sep) ? cwd : cwd + path.sep
  if (resolved !== cwd && !resolved.startsWith(base)) {
    throw new Error(`Path escapes project directory: ${rel}`)
  }
  return resolved
}

function safeRef(ref: string): string {
  if (!REF_RE.test(ref)) throw new Error(`Invalid git ref: ${ref}`)
  return ref
}

function safeTreePath(p: string): string {
  if (path.isAbsolute(p) || p.split("/").includes("..")) {
    throw new Error(`Invalid tree path: ${p}`)
  }
  return p
}

export default async function (pi: ExtensionAPI) {
  const exec = (cmd: string, args: string[], cwd: string, signal?: AbortSignal) =>
    pi.exec(cmd, args, { cwd, signal, timeout: 30_000 })

  pi.registerTool({
    name: "read_file",
    label: "Read file",
    description: "Read the contents of a file in the project.",
    parameters: Type.Object({
      path: Type.String({ description: "File path relative to project root" }),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const p = safeProjectPath(ctx.cwd, params.path)
        const content = await fs.readFile(p, "utf-8")
        return ok(content)
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "grep_search",
    label: "Grep search",
    description: "Search for a pattern in file contents. Pattern is a POSIX extended regex.",
    parameters: Type.Object({
      pattern: Type.String({ description: "Search pattern (extended regex)" }),
      path: Type.Optional(Type.String({ description: "File or directory to search (default: project root)" })),
      case_insensitive: Type.Optional(Type.Boolean({ description: "Case-insensitive match" })),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const searchPath = params.path ? safeProjectPath(ctx.cwd, params.path) : ctx.cwd
        const args = ["-r", "-n", "--include=*"]
        if (params.case_insensitive) args.push("-i")
        args.push("--", params.pattern, searchPath)
        const r = await exec("grep", args, ctx.cwd, signal)
        return ok(r.stdout || "(no matches)")
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "glob_find",
    label: "Glob find",
    description: "Find tracked (and untracked non-ignored) files by glob pattern via git ls-files. Examples: '*.ts', 'src/**/*.py'.",
    parameters: Type.Object({
      pattern: Type.String({ description: "Glob pattern, e.g. '*.ts' or 'src/**/*.py'" }),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const r = await exec(
          "git",
          ["ls-files", "--cached", "--others", "--exclude-standard", "--", params.pattern],
          ctx.cwd,
          signal,
        )
        return ok(r.stdout || "(no matches)")
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "list_dir",
    label: "List directory",
    description: "List the contents of a directory.",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Directory path relative to project root (default: project root)" })),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const dirPath = params.path ? safeProjectPath(ctx.cwd, params.path) : ctx.cwd
        const r = await exec("ls", ["-la", dirPath], ctx.cwd, signal)
        return ok(r.stdout)
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "tree",
    label: "Directory tree",
    description: "Show recursive directory structure, excluding .git.",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Path relative to project root (default: project root)" })),
      depth: Type.Optional(Type.Integer({ minimum: 1, maximum: 10, description: "Max depth (default: 3)" })),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const dirPath = params.path ? safeProjectPath(ctx.cwd, params.path) : ctx.cwd
        const depth = String(params.depth ?? 3)
        const r = await exec(
          "find",
          [dirPath, "-maxdepth", depth, "-not", "-path", "*/.git/*", "-print"],
          ctx.cwd,
          signal,
        )
        return ok(r.stdout)
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "git_log",
    label: "Git log",
    description: "Show recent commits.",
    parameters: Type.Object({
      n: Type.Optional(Type.Integer({ minimum: 1, maximum: 100, description: "Number of commits (default: 20)" })),
      path: Type.Optional(Type.String({ description: "Show only commits touching this path" })),
      ref: Type.Optional(Type.String({ description: "Branch or ref to log (default: HEAD)" })),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const args = ["log", "--oneline", `--max-count=${params.n ?? 20}`]
        if (params.ref) args.push(safeRef(params.ref))
        if (params.path) {
          args.push("--")
          args.push(safeProjectPath(ctx.cwd, params.path))
        }
        const r = await exec("git", args, ctx.cwd, signal)
        return ok(r.stdout || "(no commits)")
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "git_show",
    label: "Git show",
    description: "Show the diff and message for a specific commit.",
    parameters: Type.Object({
      ref: Type.String({ description: "Commit hash, branch name, or tag" }),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const r = await exec("git", ["show", "--stat", "-p", safeRef(params.ref)], ctx.cwd, signal)
        return ok(r.code === 0 ? r.stdout : `Error: ${r.stderr}`)
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "git_blame",
    label: "Git blame",
    description: "Show who last modified each line of a file.",
    parameters: Type.Object({
      path: Type.String({ description: "File path relative to project root" }),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const p = safeProjectPath(ctx.cwd, params.path)
        const r = await exec("git", ["blame", "--", p], ctx.cwd, signal)
        return ok(r.code === 0 ? r.stdout : `Error: ${r.stderr}`)
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "git_diff",
    label: "Git diff",
    description: "Show differences between commits, branches, or working tree.",
    parameters: Type.Object({
      from_ref: Type.Optional(Type.String({ description: "Base ref (omit for working tree vs index)" })),
      to_ref: Type.Optional(Type.String({ description: "Target ref (omit for index vs working tree)" })),
      path: Type.Optional(Type.String({ description: "Limit diff to this path" })),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const args = ["diff"]
        if (params.from_ref) args.push(safeRef(params.from_ref))
        if (params.to_ref) args.push(safeRef(params.to_ref))
        if (params.path) {
          args.push("--")
          args.push(safeProjectPath(ctx.cwd, params.path))
        }
        const r = await exec("git", args, ctx.cwd, signal)
        return ok(r.stdout || "(no differences)")
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "git_branch",
    label: "Git branches",
    description: "List all local and remote branches, showing which is current.",
    parameters: Type.Object({}),
    async execute(id, params, signal, _update, ctx) {
      try {
        const r = await exec("git", ["branch", "-a", "-v"], ctx.cwd, signal)
        return ok(r.stdout || "(no branches)")
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "git_show_file",
    label: "Git show file",
    description: "Read a file from a specific branch or commit without checking it out.",
    parameters: Type.Object({
      ref: Type.String({ description: "Branch name, tag, or commit hash (e.g. 'main', 'feature/foo', 'abc123')" }),
      path: Type.String({ description: "File path within the repository (relative to repo root, not project root)" }),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        const ref = safeRef(params.ref)
        const filePath = safeTreePath(params.path)
        const r = await exec("git", ["show", `${ref}:${filePath}`], ctx.cwd, signal)
        return ok(r.code === 0 ? r.stdout : `Error: ${r.stderr}`)
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })

  pi.registerTool({
    name: "write_file",
    label: "Write file",
    description: "Write content to a file. Only paths inside docs/patchbay/ are permitted — all others are rejected at the tool layer.",
    parameters: Type.Object({
      path: Type.String({ description: "File path relative to project root — must be inside docs/patchbay/" }),
      content: Type.String({ description: "Content to write" }),
    }),
    async execute(id, params, signal, _update, ctx) {
      try {
        if (path.isAbsolute(params.path)) {
          throw new Error(`Write rejected: absolute paths not allowed`)
        }
        const docsBase = path.join(ctx.cwd, "docs", "patchbay")
        const resolved = path.resolve(ctx.cwd, params.path)
        const base = docsBase.endsWith(path.sep) ? docsBase : docsBase + path.sep
        if (resolved !== docsBase && !resolved.startsWith(base)) {
          throw new Error(`Write rejected: path must be inside docs/patchbay/ (got ${params.path})`)
        }
        await fs.mkdir(path.dirname(resolved), { recursive: true })
        await fs.writeFile(resolved, params.content, "utf-8")
        return ok(`Written: ${resolved}`)
      } catch (e: any) {
        return ok(`Error: ${e.message}`)
      }
    },
  })
}
