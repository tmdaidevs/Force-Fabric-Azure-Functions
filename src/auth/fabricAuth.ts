import {
  DefaultAzureCredential,
  ManagedIdentityCredential,
  ClientSecretCredential,
  InteractiveBrowserCredential,
  AzureCliCredential,
  VisualStudioCodeCredential,
  DeviceCodeCredential,
} from "@azure/identity";
import type { TokenCredential, AccessToken } from "@azure/identity";

const FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default";
export const SQL_SCOPE = "https://database.windows.net/.default";
export const KUSTO_SCOPE = "https://kusto.kusto.windows.net/.default";

export type AuthMethod =
  | "azure_cli"
  | "interactive_browser"
  | "device_code"
  | "vscode"
  | "default"
  | "service_principal"
  | "managed_identity";

let credential: TokenCredential | null = null;
let cachedToken: AccessToken | null = null;
let currentAuthMethod: AuthMethod | null = null;
let isAuthenticated = false;

export function getAuthStatus(): { authenticated: boolean; method: AuthMethod | null } {
  return { authenticated: isAuthenticated, method: currentAuthMethod };
}

export function requireAuth(): void {
  if (!isAuthenticated || !credential) {
    throw new Error(
      "Not authenticated. In Azure Functions mode, set FABRIC_AUTH_METHOD and credentials in Application Settings."
    );
  }
}

/**
 * Server-side auto-initialization for Azure Functions.
 * Reads auth config from environment variables — no interactive login needed.
 */
export async function initServerAuth(): Promise<string> {
  const method = (process.env.FABRIC_AUTH_METHOD || "default") as AuthMethod;
  const tenantId = process.env.FABRIC_TENANT_ID;
  const clientId = process.env.FABRIC_CLIENT_ID;
  const clientSecret = process.env.FABRIC_CLIENT_SECRET;

  return login(method, { tenantId, clientId, clientSecret });
}

export async function login(method: AuthMethod, options?: {
  tenantId?: string;
  clientId?: string;
  clientSecret?: string;
}): Promise<string> {
  credential = null;
  cachedToken = null;
  isAuthenticated = false;
  currentAuthMethod = null;

  switch (method) {
    case "managed_identity":
      credential = options?.clientId
        ? new ManagedIdentityCredential({ clientId: options.clientId })
        : new ManagedIdentityCredential();
      break;

    case "service_principal":
      if (!options?.clientId || !options?.clientSecret || !options?.tenantId) {
        throw new Error("Service Principal requires tenantId, clientId, and clientSecret.");
      }
      credential = new ClientSecretCredential(
        options.tenantId,
        options.clientId,
        options.clientSecret,
      );
      break;

    case "azure_cli":
      credential = new AzureCliCredential();
      break;

    case "interactive_browser":
      credential = new InteractiveBrowserCredential({
        tenantId: options?.tenantId,
        clientId: options?.clientId,
      });
      break;

    case "device_code":
      credential = new DeviceCodeCredential({
        tenantId: options?.tenantId,
        clientId: options?.clientId,
        userPromptCallback: (info) => {
          console.error(`\n🔑 Device Code Auth: ${info.message}\n`);
        },
      });
      break;

    case "vscode":
      credential = new VisualStudioCodeCredential({
        tenantId: options?.tenantId,
      });
      break;

    case "default":
      credential = new DefaultAzureCredential();
      break;

    default:
      throw new Error(
        `Unknown auth method "${method}". Available: managed_identity, service_principal, azure_cli, default`
      );
  }

  try {
    cachedToken = await credential.getToken(FABRIC_SCOPE);
    if (!cachedToken) {
      throw new Error("No token received");
    }
    isAuthenticated = true;
    currentAuthMethod = method;
    return `Authenticated via "${method}". Token valid until ${new Date(cachedToken.expiresOnTimestamp).toISOString()}.`;
  } catch (error) {
    credential = null;
    const msg = error instanceof Error ? error.message : String(error);
    throw new Error(`Authentication failed with method "${method}": ${msg}`);
  }
}

export function logout(): string {
  credential = null;
  cachedToken = null;
  isAuthenticated = false;
  const prev = currentAuthMethod;
  currentAuthMethod = null;
  return prev
    ? `Logged out (was authenticated via "${prev}").`
    : "Not currently logged in.";
}

export async function getAccessToken(): Promise<string> {
  requireAuth();

  // Reuse cached token if still valid (with 5 min buffer)
  if (cachedToken && cachedToken.expiresOnTimestamp > Date.now() + 5 * 60 * 1000) {
    return cachedToken.token;
  }

  cachedToken = await credential!.getToken(FABRIC_SCOPE);
  if (!cachedToken) {
    isAuthenticated = false;
    throw new Error(
      "Token refresh failed. Please use `auth_login` to re-authenticate."
    );
  }
  return cachedToken.token;
}

export async function getTokenForScope(scope: string): Promise<string> {
  requireAuth();
  const token = await credential!.getToken(scope);
  if (!token) {
    throw new Error(`Failed to acquire token for scope "${scope}".`);
  }
  return token.token;
}
