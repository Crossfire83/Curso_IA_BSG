STANDARD_PROMPT = """
Perform the following:
1. **Detect the project type**: Look at the project root for build files (pom.xml, package.json, go.mod, Cargo.toml, requirements.txt, build.gradle, etc.) to determine the language and framework. Identify the project name and version from the build file.
2. **Find all API controllers and reconstruct the FULL exposed URLs**:
   - Search for classes/files that expose HTTP endpoints.
   - For each controller, you MUST reconstruct the complete URL path by concatenating:
     a. The class-level or router-level base path (e.g., `@RequestMapping("/api/v1/orders")` on the class).
     b. The method-level path (e.g., `@GetMapping("/{id}/status")` on the method).
     c. Any servlet context path or server prefix from config files (e.g., `server.servlet.context-path` in application.yml).
   - The final exposed URL is the concatenation of ALL these segments. For example:
     context-path `/service` + class path `/api/v1/orders` + method path `/{id}/status` → `/service/api/v1/orders/{id}/status` (GET)
   - For file-based routing frameworks (Next.js, Nuxt, SvelteKit), derive the URL from the file path in the project structure.
   - Include the HTTP method for each endpoint.

3. **Find all API client classes/objects and reconstruct the FULL dependency URLs**:
   - Search for classes/files that make outbound HTTP calls.
   - For each client, reconstruct the full URL by:
     a. Finding the base URL — it may be a string constant, a `@Value("${property}")` injection, a constructor parameter, or a config entry.
     b. Resolving any property placeholders (`${...}`) by looking up the corresponding key in YAML, properties, or XML config files present in the codebase.
     c. Concatenating the resolved base URL with any path segments appended in code (e.g., `baseUrl + "/api/v1/users/" + userId`).
     d. If the base URL references an environment variable or a property not found in the codebase, output `<UNKNOWN:property.name>` for that segment.
   - Include the HTTP method for each call if determinable.
   - **CRITICAL — ENUMERATE ALL URL COMBINATIONS**: For each outbound HTTP call, the base URL may resolve to MULTIPLE values depending on:
     a. Environment-specific config profiles (e.g., `application-dev.yml` vs `application-prod.yml` vs `application-int.yml`).
     b. Feature flags or toggles (e.g., `isApigeeEnabled`, `useProxy`) that switch between a gateway URL and a direct URL.
     c. Multiple config properties that could supply the base URL (e.g., a Feign client URL vs a RestTemplate URL for the same service).
     You MUST produce a separate entry in `depends` for EVERY resolvable combination of base URL + path. Do NOT collapse them into one entry or pick just one variant.

4. **Extract API dependency details** from each client:
   - Service name (derived from class/file name)
   - Base URL(s) and environment-specific URLs (resolve from config files when possible)
   - All endpoints (full reconstructed paths, HTTP methods)
   - Authentication method used (Bearer, API Key, OAuth, etc.)
   - Any request/response content types

5. **Discover URL configurations from ALL sources**: Search broadly for where external URLs are defined:
   - YAML/YML files (e.g., Spring config, Kubernetes manifests, custom configs)
   - JSON config files
   - Properties files (.properties, .ini) — but NOT environment files (see restriction below)
   - XML config files
   - TOML files
   - Hardcoded URLs in source code (string constants, enums, static fields)
   - Docker compose files for service dependencies
   - Terraform/CloudFormation/infrastructure files
   - Any other file that contains URL patterns (https://, http://)
   **RESTRICTION — DO NOT READ ENVIRONMENT FILES**: Skip any file whose name starts with `.env`.

6. **Dual URL handling — EXPAND ALL COMBINATIONS**: A single endpoint may be reachable through MULTIPLE URL paths (gateway vs direct, per-environment, per-config-profile). You MUST:
   - For each outbound call, find ALL base URL values across ALL config files and profiles.
   - Cross-multiply: if a call has 3 possible base URLs and 2 path variants, produce 6 entries.
   - Label each entry with its routing type or source (e.g., `"via": "apigee"`, `"via": "direct"`, `"profile": "prod"`).
   - Look for flags or config properties that switch between variants (e.g., `isApigeeEnabled`, `useProxy`, feature flags).
   - Do NOT deduplicate or summarize — list every concrete URL variant as its own entry.

7. **Generate a JSON result** containing:
   - `projectName`
   - `projectVersion`
   - `exposes`: array of strings `"GET /full/path"` for each URL this project serves
   - `depends`: array of strings `"GET https://full-resolved-url/path"` for each URL this project calls — one entry PER URL VARIANT (expand all base URL × path combinations)
   - `success: true|false`
   **RESTRICTION — DO NOT GENERATE ANYTHING ELSE**: Be very brief and just generate this json, do not add any more text or summaries, also do not generate the text as a markdown format, just answer with the raw, unformatted, plain text json.
"""