import { spawn } from "node:child_process";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { Type } from "typebox";

const CLI_PATH = process.env.CLI_PATH ?? process.env.CORPUS_CLI_PATH ?? "corpus/cli.py";
const CHUNK_HELPER_PATH = process.env.CHUNK_HELPER_PATH ?? process.env.CORPUS_CHUNK_HELPER_PATH ?? "plugins/corpus-query/chunk_query.py";
const CLI_TIMEOUT_MS = 120_000;
const WORKER_SOCKET = process.env.CORPUS_WORKER_SOCKET ?? "/tmp/corpus-worker.sock";
const SPAWN_FALLBACK = process.env.CORPUS_QUERY_MODE !== "socket";

type CliResult = {
  content: Array<{ type: "text"; text: string }>;
};

function jsonResult(value: unknown): CliResult {
  return {
    content: [{ type: "text", text: JSON.stringify(value, null, 2) }],
  };
}

function runPython(scriptPath: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [scriptPath, ...args], {
      stdio: ["ignore", "pipe", "pipe"],
      timeout: CLI_TIMEOUT_MS,
    });
    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (chunk: Buffer | string) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk: Buffer | string) => {
      stderr += chunk.toString();
    });
    child.on("error", (error) => {
      reject(new Error(`corpus cli could not start: ${error.message}`));
    });
    child.on("close", (code, signal) => {
      if (code === 0) {
        resolve(stdout);
        return;
      }
      const detail = stderr.trim() || stdout.trim() || `process terminated by ${signal ?? "unknown signal"}`;
      reject(new Error(`corpus cli failed (exit ${code ?? "null"}): ${detail}`));
    });
  });
}

function execCli(args: string[]): Promise<string> {
  return runPython(CLI_PATH, args);
}

function execChunkHelper(args: string[]): Promise<string> {
  return runPython(CHUNK_HELPER_PATH, args);
}

async function runJsonCli(args: string[]): Promise<CliResult> {
  const output = await execCli(args);
  try {
    return jsonResult(JSON.parse(output));
  } catch (error) {
    throw new Error(
      `corpus cli returned invalid JSON: ${error instanceof Error ? error.message : String(error)}; output: ${output.slice(0, 500)}`,
    );
  }
}

async function runChunkHelperJson(args: string[]): Promise<CliResult> {
  const output = await execChunkHelper(args);
  try {
    return jsonResult(JSON.parse(output));
  } catch (error) {
    throw new Error(
      `chunk helper returned invalid JSON: ${error instanceof Error ? error.message : String(error)}; output: ${output.slice(0, 500)}`,
    );
  }
}

async function runWorker(command: string, params: unknown, fallback: () => Promise<CliResult>): Promise<CliResult> {
  const request = { schema_version: 1, request_id: `${Date.now()}-${Math.random().toString(16).slice(2)}`, command, params };
  if (process.env.CORPUS_QUERY_MODE !== "spawn") {
    try {
      const net = await import("node:net");
      const response = await new Promise<any>((resolve, reject) => {
        const c = net.createConnection(WORKER_SOCKET); let buf = "";
        c.setTimeout(CLI_TIMEOUT_MS); c.on("connect", () => c.write(JSON.stringify(request) + "\n"));
        c.on("data", d => { buf += d.toString(); const i = buf.indexOf("\n"); if (i >= 0) { c.end(); resolve(JSON.parse(buf.slice(0,i))); } });
        c.on("error", reject); c.on("timeout", () => reject(new Error("worker timeout")));
      });
      if (!response.ok) throw new Error(response.error?.message ?? "worker error");
      return jsonResult(response.result);
    } catch (e) { if (!SPAWN_FALLBACK) throw e; }
  }
  return fallback();
}

function scoreEventId(): string { return `sev_${Date.now()}_${Math.random().toString(36).slice(2)}`; }

const queryParameters = Type.Object({
  project: Type.String({ description: "Project slug, for example ar-review." }),
  query: Type.String({ description: "Natural-language topic, title, or keyword query." }),
  top_k: Type.Optional(Type.Integer({ minimum: 1, maximum: 50, default: 10 })),
  pmid_filter: Type.Optional(
    Type.String({ description: "Comma-separated PMID whitelist, for example 12345678,23456789." }),
  ),
  rerank_mode: Type.Optional(
    Type.Enum({ linear: "linear", dashscope: "dashscope" }, {
      description: "linear uses local weighted scores; dashscope uses the Qwen reranker.",
    }),
  ),
});

const searchSelfParameters = Type.Object({
  project: Type.String({ description: "Project slug used to filter self-written documents." }),
  query: Type.String({ description: "Natural-language query over drafts, reviews, or proposals." }),
  top_k: Type.Optional(Type.Integer({ minimum: 1, maximum: 50, default: 5 })),
  level: Type.Optional(
    Type.Integer({ minimum: 0, maximum: 3, description: "Heading level filter: 0=preface, 1=H1, 2=H2, 3=H3." }),
  ),
  expand_context: Type.Optional(
    Type.Boolean({ default: false, description: "Include parent and sibling chunks for tree-aware retrieval." }),
  ),
});

const scoreParameters = Type.Object({
  project: Type.String({ description: "Project slug containing the paper." }),
  pmid: Type.String({ description: "PubMed identifier of the paper to score." }),
  criterion_validity: Type.Number({ minimum: 0, maximum: 1 }),
  outcome_reliability: Type.Number({ minimum: 0, maximum: 1 }),
  conclusion_data_consistency: Type.Number({ minimum: 0, maximum: 1 }),
  notes: Type.Optional(Type.String({ description: "Optional curator notes saved with the score." })),
});

const getChunkParameters = Type.Object({
  chunk_id: Type.String({
    description:
      "Chunk ID, for example 'review_ai_fall_elderly__ch5__5.1__5.1.1__p1' or a V1 chunk id.",
  }),
  include: Type.Optional(
    Type.Array(
      Type.Enum({ text: "text", snippet: "snippet", metadata: "metadata", siblings: "siblings", children: "children", parent: "parent" }),
      { description: "What to include in response (default: text + metadata)." },
    ),
  ),
});

const similarLevelParameters = Type.Object({
  chunk_id: Type.String({
    description: "Reference chunk to find similar-level content for.",
  }),
  top_k: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, default: 5 })),
  same_doc: Type.Optional(
    Type.Boolean({ default: false, description: "Restrict to the same document (default: cross-document)." }),
  ),
});

export default definePluginEntry({
  id: "corpus-query-tool",
  name: "Corpus Query Tool",
  description: "Expose the corpus CLI as agent-callable search and scoring tools.",
  register(api) {
    api.registerTool({
      name: "corpus_query",
      description:
        "Hybrid vector and keyword search over the unified literature corpus. Returns relevance, quality, venue, and primary/supporting/background recommendations. Use to check known literature for a project or retrieve evidence-graded papers.",
      parameters: queryParameters,
      async execute(_id, params) {
        const args = ["query", "--project", params.project, "--query", params.query];
        if (params.top_k !== undefined) args.push("--top", String(params.top_k));
        if (params.pmid_filter) args.push("--pmid-filter", params.pmid_filter);
        if (params.rerank_mode) args.push("--rerank-mode", params.rerank_mode);
        return runWorker("query", params, () => runJsonCli(args));
      },
    });

    api.registerTool({
      name: "corpus_search_self",
      description:
        "Search self-written documents with Qwen embeddings and heading-aware tree rerank. Returns chunk path, heading chain, parent, and level metadata. Use level to filter headings or expand_context to include parent and siblings.",
      parameters: searchSelfParameters,
      async execute(_id, params) {
        const args = [
          "--embedding-mode",
          "qwen",
          "search-self",
          "--project",
          params.project,
          "--query",
          params.query,
        ];
        if (params.top_k !== undefined) args.push("--top", String(params.top_k));
        if (params.level !== undefined) args.push("--level", String(params.level));
        if (params.expand_context) args.push("--expand-context");
        return runWorker("search", params, () => runJsonCli(args));
      },
    });

    api.registerTool({
      name: "corpus_score",
      description:
        "Recalculate a paper's quality evidence scores from criterion validity, outcome reliability, and conclusion-data consistency. The CLI persists the score and computes quality_final for curator workflows.",
      parameters: scoreParameters,
      async execute(_id, params) {
        const args = [
          "score",
          "--project",
          params.project,
          "--pmid",
          params.pmid,
          "--criterion",
          String(params.criterion_validity),
          "--outcome",
          String(params.outcome_reliability),
          "--conclusion",
          String(params.conclusion_data_consistency),
        ];
        if (params.notes) args.push("--notes", params.notes);
        return runWorker("score", {...params, score_event_id: scoreEventId()}, () => runJsonCli([...args, "--json", "--score-event-id", scoreEventId()]));
      },
    });

    api.registerTool({
      name: "corpus_get_chunk",
      description:
        "Get a single chunk's full content plus complete heading-tree context (path, heading chain, parent, children, siblings). Use this to understand WHERE a chunk sits in its document's structure before retrieving similar-level content.",
      parameters: getChunkParameters,
      async execute(_id, params) {
        const include = params.include ?? ["text", "metadata"];
        const args = ["get", "--chunk-id", params.chunk_id, "--include", include.join(",")];
        return runWorker("get", params, () => runChunkHelperJson(args));
      },
    });

    api.registerTool({
      name: "corpus_list_similar_level",
      description:
        "Given a chunk, find chunks at the SAME heading level with similar content (vector similarity + heading-chain overlap). Use to retrieve cross-document parallel sections (e.g. all 'Methods' sections across papers, all 'Limitations' sections in a review).",
      parameters: similarLevelParameters,
      async execute(_id, params) {
        const args = [
          "similar",
          "--chunk-id",
          params.chunk_id,
          "--top-k",
          String(params.top_k ?? 5),
        ];
        if (params.same_doc) args.push("--same-doc");
        return runWorker("similar", params, () => runChunkHelperJson(args));
      },
    });
  },
});
