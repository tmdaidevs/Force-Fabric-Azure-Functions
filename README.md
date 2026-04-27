# Force Fabric MCP Server — Azure Functions

> **HTTP API for Microsoft Fabric optimization** — deploy as Azure Functions, connect to Azure AI Foundry Agents and/or Copilot Studio.

This is the cloud-hosted version of [Force-Fabric-MCP-Server](https://github.com/tmdaidevs/Force-Fabric-MCP-Server). Instead of running locally via stdio, it exposes all Fabric optimization tools as HTTP endpoints on Azure Functions.

## 🏗️ Architecture

```
┌─────────────────────┐     ┌──────────────────────────┐     ┌─────────────────┐
│  Azure AI Foundry    │────▶│  Azure Functions (this)   │────▶│  Microsoft       │
│  Agent / Copilot     │     │                          │     │  Fabric APIs     │
│  Studio              │◀────│  POST /api/tools/{name}  │◀────│  (REST, SQL,     │
│                     │     │  GET  /api/openapi.json   │     │   KQL, XMLA)     │
└─────────────────────┘     └──────────────────────────┘     └─────────────────┘
                                     │
                                     ▼
                            Managed Identity / 
                            Service Principal
```

## 🚀 Quick Start

### Prerequisites

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`)
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local) (`func`)
- Node.js 20+
- An Azure subscription with a Fabric capacity

### 1. Clone & Install

```bash
git clone https://github.com/tmdaidevs/Force-Fabric-Azure-Functions.git
cd Force-Fabric-Azure-Functions
npm install
```

### 2. Configure Local Settings

Edit `local.settings.json`:

```json
{
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "node",
    "FABRIC_AUTH_METHOD": "azure_cli",
    "FABRIC_TENANT_ID": "",
    "FABRIC_CLIENT_ID": "",
    "FABRIC_CLIENT_SECRET": ""
  }
}
```

| Auth Method | When to Use | Required Env Vars |
|---|---|---|
| `default` | Auto-detect (recommended for Azure) | — |
| `managed_identity` | Running on Azure with Managed Identity | `FABRIC_CLIENT_ID` (optional, for user-assigned) |
| `service_principal` | Automated / multi-tenant | `FABRIC_TENANT_ID`, `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET` |
| `azure_cli` | Local development | — (uses `az login` session) |

### 3. Run Locally

```bash
npm run build
func start
```

Test it:

```bash
# Health check
curl http://localhost:7071/api/health

# List all tools
curl http://localhost:7071/api/tools

# Execute a tool
curl -X POST http://localhost:7071/api/tools/workspace_list \
  -H "Content-Type: application/json" \
  -d '{}'

# Get OpenAPI spec
curl http://localhost:7071/api/openapi.json
```

### 4. Deploy to Azure

```bash
# Create resources
az login
az group create --name rg-fabric-mcp --location westeurope
az storage account create --name stfabricmcp --location westeurope --resource-group rg-fabric-mcp --sku Standard_LRS
az functionapp create \
  --name func-fabric-mcp \
  --resource-group rg-fabric-mcp \
  --storage-account stfabricmcp \
  --consumption-plan-location westeurope \
  --runtime node \
  --runtime-version 20 \
  --functions-version 4

# Enable Managed Identity
az functionapp identity assign --name func-fabric-mcp --resource-group rg-fabric-mcp

# Set app settings
az functionapp config appsettings set --name func-fabric-mcp --resource-group rg-fabric-mcp \
  --settings FABRIC_AUTH_METHOD=managed_identity

# Deploy
npm run build
func azure functionapp publish func-fabric-mcp
```

### 5. Grant Fabric Permissions

The Function App's Managed Identity needs access to your Fabric workspace:

1. Go to **Fabric Portal** → **Workspace Settings** → **Manage Access**
2. Add the Function App's Managed Identity (search by Function App name)
3. Assign **Admin** or **Contributor** role

---

## 🔌 Connect to Azure AI Foundry

1. Navigate to [Azure AI Foundry](https://ai.azure.com)
2. Open your **Agent** → **Tools** → **Add Tool** → **OpenAPI**
3. Enter the URL: `https://func-fabric-mcp.azurewebsites.net/api/openapi.json`
4. Add the Function Key as authentication header (`x-functions-key`)
5. Select which tools to enable for the agent

## 🔌 Connect to Copilot Studio

1. Open [Copilot Studio](https://copilotstudio.microsoft.com)
2. Go to **Settings** → **Generative AI** → **Custom connectors**
3. Create a new connector from the OpenAPI spec URL
4. Set authentication to **API Key** with the Function Key
5. Add the connector as an action in your Copilot

---

## 📋 Available Tools

### Workspace
| Tool | Description |
|---|---|
| `workspace_list` | List all Fabric workspaces |
| `workspace_list_items` | List items in a workspace |
| `workspace_capacity_info` | List capacities |
| `fabric_optimization_report` | Full workspace optimization report |

### Lakehouse
| Tool | Description |
|---|---|
| `lakehouse_list` | List lakehouses |
| `lakehouse_list_tables` | List tables in a lakehouse |
| `lakehouse_optimization_recommendations` | Live scan with recommendations |
| `lakehouse_fix` | Apply fixes (auto-optimize, retention, etc.) |
| `lakehouse_auto_optimize` | Apply all fixes across all tables |
| `lakehouse_run_table_maintenance` | OPTIMIZE + VACUUM |

### Warehouse
| Tool | Description |
|---|---|
| `warehouse_list` | List warehouses |
| `warehouse_optimization_recommendations` | Live scan with recommendations |
| `warehouse_fix` | Apply fixes (statistics, PKs, settings) |
| `warehouse_auto_optimize` | Apply all fixes |
| `warehouse_analyze_query_patterns` | Query performance analysis |

### Eventhouse
| Tool | Description |
|---|---|
| `eventhouse_list` | List eventhouses |
| `eventhouse_optimization_recommendations` | Live scan with recommendations |
| `eventhouse_fix` | Apply fixes |
| `eventhouse_auto_optimize` | Apply all fixes |
| `eventhouse_fix_materialized_views` | Repair broken materialized views |

### Semantic Model
| Tool | Description |
|---|---|
| `semantic_model_list` | List semantic models |
| `semantic_model_optimization_recommendations` | Live scan with BPA rules |
| `semantic_model_fix` | Apply DAX/model fixes |
| `semantic_model_auto_optimize` | Apply all 19 safe fixes |

### Gateway
| Tool | Description |
|---|---|
| `gateway_list` | List gateways |
| `gateway_list_connections` | List connections |
| `gateway_optimization_recommendations` | Scan with 12 rules |
| `gateway_fix` | Apply fixes |

---

## 🔐 Security

- **Authentication**: Azure Managed Identity (recommended) or Service Principal
- **Authorization**: Azure Functions key (`x-functions-key` header)
- **Network**: Deploy with VNet integration for private Fabric endpoints
- No secrets stored in code — all credentials via Azure App Settings or Key Vault references

## 📄 License

MIT — see [LICENSE](LICENSE)
