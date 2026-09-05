import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import { join, relative, resolve } from "node:path";
import type { MemoryFile } from "./types.js";
import { MEMORY_FILE_PATTERNS } from "./config.js";
import { matchGlob } from "./scope.js";

// Openclaw's session-memory hook writes `memory/YYYY-MM-DD-HHMM.md` transcripts;
// skip those — cognee's session cache already holds the same turns and we'd
// double-ingest on /improve.
const SESSION_MEMORY_FILE_RE = /^\d{4}-\d{2}-\d{2}-\d{4}\.md$/;

export function hashText(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

/**
 * True when a workspace-relative path matches any exclude pattern.
 * Uses the same glob syntax as `scopeRouting.pattern`.
 */
function isExcluded(relPath: string, excludePatterns: string[]): boolean {
  return excludePatterns.some((pattern) => matchGlob(pattern, relPath));
}

/**
 * Scan the workspace for memory markdown files.
 *
 * `excludePatterns` skips paths before they are read. A directory that matches
 * is not descended into, so excluding a large tree (e.g. `memory/dreaming/**`)
 * costs one match instead of one read per file.
 */
export async function collectMemoryFiles(
  workspaceDir: string,
  excludePatterns: string[] = [],
): Promise<MemoryFile[]> {
  const files: MemoryFile[] = [];

  for (const pattern of MEMORY_FILE_PATTERNS) {
    const target = resolve(workspaceDir, pattern);
    const relPath = relative(workspaceDir, target);

    if (isExcluded(relPath, excludePatterns)) continue;

    try {
      const stat = await fs.stat(target);

      if (stat.isFile() && target.endsWith(".md")) {
        const content = await fs.readFile(target, "utf-8");
        files.push({
          path: relPath,
          absPath: target,
          content,
          hash: hashText(content),
        });
      } else if (stat.isDirectory()) {
        const entries = await scanDir(target, workspaceDir, excludePatterns);
        files.push(...entries);
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        throw error;
      }
    }
  }

  return files;
}

async function scanDir(
  dir: string,
  workspaceDir: string,
  excludePatterns: string[],
): Promise<MemoryFile[]> {
  const files: MemoryFile[] = [];

  const entries = await fs.readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const absPath = join(dir, entry.name);
    const relPath = relative(workspaceDir, absPath);

    if (isExcluded(relPath, excludePatterns)) continue;

    if (entry.isDirectory()) {
      const nested = await scanDir(absPath, workspaceDir, excludePatterns);
      files.push(...nested);
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      if (SESSION_MEMORY_FILE_RE.test(entry.name)) continue;
      const content = await fs.readFile(absPath, "utf-8");
      files.push({
        path: relPath,
        absPath,
        content,
        hash: hashText(content),
      });
    }
  }

  return files;
}
