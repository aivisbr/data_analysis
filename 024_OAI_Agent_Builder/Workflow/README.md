# OpenAI Agent Workflow Example

The file **Workflow.png** illustrates the overall structure and execution flow of the agent workflow.

Since the platform does not currently support exporting workflows in `.json` or other machine-readable formats, the file **AgentSDK.ts** is included to provide visibility into the prompts and configuration used to construct this workflow.

Additional notes:

- **[CSP MCP](https://aivis-csp-data.hf.space/gradio_api/mcp/sse)** is included purely for experimental purposes, demonstrating that custom Model Context Protocol (MCP) integrations can be incorporated into the workflow.
- The `id` specified in the `fileSearchTool` configuration (see **AgentSDK.ts**) refers to a **vector store** that must be created in **OpenAI Storage**. For example:  
  https://platform.openai.com/storage/vector_stores/vs_680b7e6737e881918d74225fde544563
