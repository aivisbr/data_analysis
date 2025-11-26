# How to connect this MCP server to the MCP host
1. In the terminal install the required Python packages:  
```pip install -r requirements.txt```
2. In the terminal run `mcp_example.py`:  
```python mcp_example.py```  
Terminal will show something like that (port number could be different):
```
INFO:     Started server process [53460]
INFO:     Waiting for application startup.
INFO     StreamableHTTP session manager started
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```
3. Then in the terminal launch [Ngrok](https://ngrok.com/) (you need to install it and configure if it is not yet done):
```
ngrok http http://127.0.0.1:8000
```
Terminal (for Free plan users) will show something like that:
```
Session Status                online
Account                       your-e-mail@domain.com (Plan: Free)
Version                       3.24.0-msix
Region                        Europe (eu)
Latency                       34ms
Web Interface                 http://127.0.0.1:4040
Forwarding                    https://CODE.ngrok-free.app -> http://localhost:8000
```
4. Then go to the MCP Host ([ChatGPT](https://chatgpt.com/), [Claude](https://claude.ai/settings/connectors), [Mistral](https://chat.mistral.ai/connections) etc.) and add this MCP Server as a connector (using `https://CODE.ngrok-free.app/mcp` as a MCP server URL).
