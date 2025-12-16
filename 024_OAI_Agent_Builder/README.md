# Content
- Backend  
Backend was created using [Google Antigravity](https://antigravity.google/) with the following prompt:  
> _Create a server-side ChatKit integration using Flask to serve a chat widget. The backend will handle session creation by communicating with the OpenAI API, and the frontend will embed the ChatKit widget. Here's the Chatkit documentation*: <https://platform.openai.com/docs/guides/chatkit>_

*I copied the full documentation text.
- Frontend  
Frontend was created using [Bolt.new](https://bolt.new/) with the following prompt:
> _Create a landing page for a technology company called "Scandic Fusion". In the bottom left corner of the page implement chat button and when user clicks on it it will uses ChatKit. Here's a documentation* about it (implement only UI side, because I have already a backend server  https://8e5c5acda7a7.ngrok-free.app/api/chatkit/session): <https://platform.openai.com/docs/guides/chatkit>_

*I copied the full documentation text.

# Usage

To embed the chat widget into a web page, follow these steps:

## 1. Create a workflow
1. Create a workflow using the [Agent Builder](https://platform.openai.com/agent-builder).
2. Copy the generated **Workflow ID** and add it to the backend configuration:
   - Create or update the `.env.local` file (see `.env.example`).
   - Set the `WORKFLOW_ID` value accordingly.

## 2. Start the backend
1. Ensure the following environment variables are defined in `.env.local`:
   - `WORKFLOW_ID`
   - `OPENAI_API_KEY` (create one at https://platform.openai.com/api-keys)
2. Start the backend server:
   ```bash
   python app.py
   ```

## 3. Expose the backend using ngrok

1. Open a new terminal and run:

   ```bash
   ngrok http http://127.0.0.1:5000
   ```

   (The host and port must match the backend address from step 2.)
2. After ngrok starts, it will display a forwarding URL similar to:

   ```
   Forwarding https://38a30bf4d507.ngrok-free.app -> http://127.0.0.1:5000
   ```

   Copy the HTTPS URL — it will be used by the frontend.

## 4. Start the frontend

1. Update the backend API URL in `ChatWidget.tsx` using the ngrok URL from step 3:

   ```ts
   const res = await fetch(
     'https://38a30bf4d507.ngrok-free.app/api/chatkit/session',
     { ... }
   )
   ```
2. To run the frontend locally:

   1. Navigate to the `Frontend` directory.
   2. Install dependencies and start the development server:

      ```bash
      npm install
      npm run dev
      ```
   3. The terminal will display a local URL, for example:

      ```
      Local: http://localhost:5173/
      ```

      Open this URL in your browser to start chatting.
3. If you deploy the frontend using platforms such as [Bolt.new](https://bolt.new/) or [Lovable](https://lovable.dev/):

   1. Add the deployed domain to the OpenAI **Domain Allowlist**:
      [https://platform.openai.com/settings/organization/security/domain-allowlist](https://platform.openai.com/settings/organization/security/domain-allowlist)
   2. The chat widget will only function after the domain has been allowlisted.


# Agent builder resources
- Announcement article: [Introducing AgentKit](https://openai.com/index/introducing-agentkit/)
- Agent Builder [documentation](https://platform.openai.com/docs/guides/agent-builder)
- ChatKit [documentation](https://platform.openai.com/docs/guides/chatkit)
- [ChatKit Studio](https://chatkit.studio/)
- [Widget builder](https://widgets.chatkit.studio/)
- [Playground](https://chatkit.studio/playground)
- Platform OpenAI: [Domain allowlist](https://platform.openai.com/settings/organization/security/domain-allowlist)
