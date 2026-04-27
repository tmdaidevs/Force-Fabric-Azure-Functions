import { app, HttpRequest, HttpResponseInit, InvocationContext } from "@azure/functions";
import { allTools, getToolByName, AUTH_TOOL_NAMES } from "./tools/index.js";
import { initServerAuth, requireAuth } from "./auth/fabricAuth.js";

// Auto-initialize auth on cold start using Managed Identity / env vars
let authInitialized = false;

async function ensureAuth(): Promise<void> {
  if (!authInitialized) {
    await initServerAuth();
    authInitialized = true;
  }
}

// Main tool execution endpoint: POST /api/tools/{toolName}
async function executeTool(req: HttpRequest, context: InvocationContext): Promise<HttpResponseInit> {
  try {
    const toolName = req.params.toolName;
    if (!toolName) {
      return { status: 400, jsonBody: { error: "Missing tool name in URL path" } };
    }

    const tool = getToolByName(toolName);
    if (!tool) {
      return {
        status: 404,
        jsonBody: {
          error: `Tool "${toolName}" not found`,
          available: allTools.map(t => t.name),
        },
      };
    }

    // Parse request body
    let args: Record<string, unknown> = {};
    try {
      const body = await req.text();
      if (body) {
        args = JSON.parse(body);
      }
    } catch {
      return { status: 400, jsonBody: { error: "Invalid JSON body" } };
    }

    // Ensure auth for non-auth tools
    if (!AUTH_TOOL_NAMES.has(toolName)) {
      await ensureAuth();
      requireAuth();
    }

    context.log(`Executing tool: ${toolName}`);
    const result = await tool.handler(args);

    return {
      status: 200,
      jsonBody: { tool: toolName, result },
      headers: { "Content-Type": "application/json" },
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    context.error(`Tool execution error: ${message}`);
    return {
      status: 500,
      jsonBody: { error: message },
    };
  }
}

// List all available tools: GET /api/tools
async function listTools(req: HttpRequest, context: InvocationContext): Promise<HttpResponseInit> {
  const tools = allTools.map(t => ({
    name: t.name,
    description: t.description,
    parameters: t.inputSchema,
  }));

  return {
    status: 200,
    jsonBody: { count: tools.length, tools },
    headers: { "Content-Type": "application/json" },
  };
}

// Health check: GET /api/health
async function health(req: HttpRequest, context: InvocationContext): Promise<HttpResponseInit> {
  return {
    status: 200,
    jsonBody: {
      status: "healthy",
      service: "Force Fabric MCP Server",
      version: "1.0.0",
      authInitialized,
      toolCount: allTools.length,
    },
  };
}

// OpenAPI spec: GET /api/openapi.json
async function openApiSpec(req: HttpRequest, context: InvocationContext): Promise<HttpResponseInit> {
  const paths: Record<string, unknown> = {};

  for (const tool of allTools) {
    if (AUTH_TOOL_NAMES.has(tool.name)) continue; // Skip auth tools for external consumers

    const properties = tool.inputSchema.properties ?? {};
    const required = tool.inputSchema.required ?? [];

    const schemaProps: Record<string, unknown> = {};
    for (const [key, prop] of Object.entries(properties)) {
      const p = prop as Record<string, unknown>;
      schemaProps[key] = {
        type: p.type,
        description: p.description,
        ...(p.items ? { items: p.items } : {}),
        ...(p.enum ? { enum: p.enum } : {}),
      };
    }

    paths[`/api/tools/${tool.name}`] = {
      post: {
        operationId: tool.name,
        summary: tool.description,
        requestBody: {
          required: required.length > 0,
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: schemaProps,
                required: required.length > 0 ? required : undefined,
              },
            },
          },
        },
        responses: {
          "200": {
            description: "Tool execution result",
            content: {
              "application/json": {
                schema: {
                  type: "object",
                  properties: {
                    tool: { type: "string" },
                    result: { type: "string" },
                  },
                },
              },
            },
          },
          "400": { description: "Bad request" },
          "500": { description: "Execution error" },
        },
      },
    };
  }

  const spec = {
    openapi: "3.0.3",
    info: {
      title: "Force Fabric MCP Server",
      version: "1.0.0",
      description:
        "HTTP API for Microsoft Fabric optimization — Lakehouse, Warehouse, Eventhouse, Semantic Models. Use as a custom tool in Azure AI Foundry Agents or Copilot Studio.",
    },
    servers: [
      {
        url: `${req.headers.get("x-forwarded-proto") || "https"}://${req.headers.get("host") || "localhost:7071"}`,
        description: "Current deployment",
      },
    ],
    paths,
    security: [{ apiKey: [] }],
    components: {
      securitySchemes: {
        apiKey: {
          type: "apiKey",
          in: "header",
          name: "x-functions-key",
        },
      },
    },
  };

  return {
    status: 200,
    jsonBody: spec,
    headers: { "Content-Type": "application/json" },
  };
}

// Register Azure Function routes
app.http("executeTool", {
  methods: ["POST"],
  authLevel: "function",
  route: "tools/{toolName}",
  handler: executeTool,
});

app.http("listTools", {
  methods: ["GET"],
  authLevel: "function",
  route: "tools",
  handler: listTools,
});

app.http("health", {
  methods: ["GET"],
  authLevel: "anonymous",
  route: "health",
  handler: health,
});

app.http("openApiSpec", {
  methods: ["GET"],
  authLevel: "anonymous",
  route: "openapi.json",
  handler: openApiSpec,
});
