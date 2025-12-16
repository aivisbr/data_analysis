import { webSearchTool, fileSearchTool, hostedMcpTool, Agent, RunContext, AgentInputItem, Runner, withTrace } from "@openai/agents";
import { OpenAI } from "openai";
import { runGuardrails } from "@openai/guardrails";
import { z } from "zod";


// Tool definitions
const webSearchPreview = webSearchTool({
  filters: {
    allowed_domains: [
      "www.scandicfusion.com"
    ]
  },
  searchContextSize: "medium",
  userLocation: {
    country: "LV",
    type: "approximate"
  }
})
const fileSearch = fileSearchTool([
  "vs_680b7e6737e881918d74225fde544563"
])
const mcp = hostedMcpTool({
  serverLabel: "CSP",
  allowedTools: [
    "CSP_data_get_topics",
    "CSP_data_get_topic_content",
    "CSP_data_get_titles",
    "CSP_data_get_query_values",
    "CSP_data_run_get_csp_data"
  ],
  requireApproval: "never",
  serverDescription: "Data from central statistical bureau of Latvia (CSP)",
  serverUrl: "https://aivis-csp-data.hf.space/gradio_api/mcp/sse"
})

// Shared client for guardrails and file search
const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

// Guardrails definitions
const guardrailsConfig = {
  guardrails: [
    { name: "Jailbreak", config: { model: "gpt-4.1-mini", confidence_threshold: 0.7 } }
  ]
};
const context = { guardrailLlm: client };

function guardrailsHasTripwire(results: any[]): boolean {
    return (results ?? []).some((r) => r?.tripwireTriggered === true);
}

function getGuardrailSafeText(results: any[], fallbackText: string): string {
    for (const r of results ?? []) {
        if (r?.info && ("checked_text" in r.info)) {
            return r.info.checked_text ?? fallbackText;
        }
    }
    const pii = (results ?? []).find((r) => r?.info && "anonymized_text" in r.info);
    return pii?.info?.anonymized_text ?? fallbackText;
}

async function scrubConversationHistory(history: any[], piiOnly: any): Promise<void> {
    for (const msg of history ?? []) {
        const content = Array.isArray(msg?.content) ? msg.content : [];
        for (const part of content) {
            if (part && typeof part === "object" && part.type === "input_text" && typeof part.text === "string") {
                const res = await runGuardrails(part.text, piiOnly, context, true);
                part.text = getGuardrailSafeText(res, part.text);
            }
        }
    }
}

async function scrubWorkflowInput(workflow: any, inputKey: string, piiOnly: any): Promise<void> {
    if (!workflow || typeof workflow !== "object") return;
    const value = workflow?.[inputKey];
    if (typeof value !== "string") return;
    const res = await runGuardrails(value, piiOnly, context, true);
    workflow[inputKey] = getGuardrailSafeText(res, value);
}

async function runAndApplyGuardrails(inputText: string, config: any, history: any[], workflow: any) {
    const guardrails = Array.isArray(config?.guardrails) ? config.guardrails : [];
    const results = await runGuardrails(inputText, config, context, true);
    const shouldMaskPII = guardrails.find((g) => (g?.name === "Contains PII") && g?.config && g.config.block === false);
    if (shouldMaskPII) {
        const piiOnly = { guardrails: [shouldMaskPII] };
        await scrubConversationHistory(history, piiOnly);
        await scrubWorkflowInput(workflow, "input_as_text", piiOnly);
        await scrubWorkflowInput(workflow, "input_text", piiOnly);
    }
    const hasTripwire = guardrailsHasTripwire(results);
    const safeText = getGuardrailSafeText(results, inputText) ?? inputText;
    return { results, hasTripwire, safeText, failOutput: buildGuardrailFailOutput(results ?? []), passOutput: { safe_text: safeText } };
}

function buildGuardrailFailOutput(results: any[]) {
    const get = (name: string) => (results ?? []).find((r: any) => ((r?.info?.guardrail_name ?? r?.info?.guardrailName) === name));
    const pii = get("Contains PII"), mod = get("Moderation"), jb = get("Jailbreak"), hal = get("Hallucination Detection"), nsfw = get("NSFW Text"), url = get("URL Filter"), custom = get("Custom Prompt Check"), pid = get("Prompt Injection Detection"), piiCounts = Object.entries(pii?.info?.detected_entities ?? {}).filter(([, v]) => Array.isArray(v)).map(([k, v]) => k + ":" + v.length), conf = jb?.info?.confidence;
    return {
        pii: { failed: (piiCounts.length > 0) || pii?.tripwireTriggered === true, detected_counts: piiCounts },
        moderation: { failed: mod?.tripwireTriggered === true || ((mod?.info?.flagged_categories ?? []).length > 0), flagged_categories: mod?.info?.flagged_categories },
        jailbreak: { failed: jb?.tripwireTriggered === true },
        hallucination: { failed: hal?.tripwireTriggered === true, reasoning: hal?.info?.reasoning, hallucination_type: hal?.info?.hallucination_type, hallucinated_statements: hal?.info?.hallucinated_statements, verified_statements: hal?.info?.verified_statements },
        nsfw: { failed: nsfw?.tripwireTriggered === true },
        url_filter: { failed: url?.tripwireTriggered === true },
        custom_prompt_check: { failed: custom?.tripwireTriggered === true },
        prompt_injection: { failed: pid?.tripwireTriggered === true },
    };
}
const MyAgentSchema = z.object({ topic: z.enum(["blog", "case studies", "career", "contacts", "technology stack"]) });
const AgentSchema = z.object({ emailFrom: z.string(), defaultTo: z.string(), defaultSubject: z.string(), defaultBody: z.string() });
const AgentSchema1 = z.object({ interest: z.boolean() });
const AgentSchema2 = z.object({ url: z.string(), summary: z.string() });
const AgentSchema3 = z.object({ summary: z.string(), url: z.string(), linkLabel: z.string() });
const myAgent = new Agent({
  name: "My agent",
  instructions: `You are an assistant dedicated exclusively to helping users with information related to Scandic Fusion ([www.scandicfusion.com](http://www.scandicfusion.com)).

### Your Scope

You may answer questions only about:

* Scandic Fusion company information
* Contacts
* Technology stack
* Services and solutions
* Case studies
* Blog posts
* Career opportunities
* Partnerships
* Customer success stories
* Any other content clearly connected to Scandic Fusion

### Out-of-Scope Rules

If a user asks for anything not related to Scandic Fusion, such as:

* Recipes
* Code generation
* Weather
* Travel advice
* General technical questions
* Personal questions
 Any topic with no clear link* to Scandic Fusion

Then you must respond with:
“This question is not related to Scandic Fusion, so I cannot assist with it.”

### Output Requirement

When the user’s query is related to Scandic Fusion, output only the specific Scandic Fusion topic they are asking about.

When the user’s query is not related, output only the refusal message.`,
  model: "gpt-4.1",
  outputType: MyAgentSchema,
  modelSettings: {
    temperature: 1,
    topP: 1,
    maxTokens: 2048,
    store: true
  }
});

const agent = new Agent({
  name: "Agent",
  instructions: "Inform user with the open positions that is mentioned in this page under the section \"Open positions\": https://www.scandicfusion.com/careers",
  model: "gpt-4.1",
  tools: [
    webSearchPreview
  ],
  modelSettings: {
    temperature: 1,
    topP: 1,
    maxTokens: 2048,
    store: true
  }
});

const agent1 = new Agent({
  name: "Agent",
  instructions: "Inform users with latest blogs or case studies mentioned here: https://www.scandicfusion.com/blog",
  model: "gpt-4.1",
  tools: [
    webSearchPreview
  ],
  modelSettings: {
    temperature: 1,
    topP: 1,
    maxTokens: 2048,
    store: true
  }
});

const agent2 = new Agent({
  name: "Agent",
  instructions: `Get contact information from this page: https://www.scandicfusion.com/contacts
Email, phone number, address.
Add follow-up question whether users wants an e-mail of HR, marketing or data protection.`,
  model: "gpt-4.1",
  tools: [
    webSearchPreview
  ],
  modelSettings: {
    temperature: 1,
    topP: 1,
    maxTokens: 2048,
    store: true
  }
});

const agent3 = new Agent({
  name: "Agent",
  instructions: "Inform user with the technology stack that Scandic Fusion works with. Use information from this site: https://www.scandicfusion.com/technology-stack or vector store data.",
  model: "gpt-4.1",
  tools: [
    fileSearch,
    webSearchPreview
  ],
  modelSettings: {
    temperature: 1,
    topP: 1,
    maxTokens: 2048,
    store: true
  }
});

const agent4 = new Agent({
  name: "Agent",
  instructions: "Prepare an e-mail for the position that user has been chosen.",
  model: "gpt-5",
  outputType: AgentSchema,
  modelSettings: {
    reasoning: {
      effort: "medium",
      summary: "auto"
    },
    store: true
  }
});

const agent5 = new Agent({
  name: "Agent",
  instructions: "Are user interested in the particular position and ready to send an e-mail?",
  model: "gpt-5",
  outputType: AgentSchema1,
  modelSettings: {
    reasoning: {
      effort: "low",
      summary: "auto"
    },
    store: true
  }
});

const agent6 = new Agent({
  name: "Agent",
  instructions: "Extract random data from the CSP and in the output give a link to these data and a summary about these data. ",
  model: "gpt-5.1",
  tools: [
    mcp
  ],
  outputType: AgentSchema2,
  modelSettings: {
    reasoning: {
      effort: "high",
      summary: "auto"
    },
    store: true
  }
});

interface AgentContext {
  inputOutputParsedUrl: string;
  inputOutputParsedSummary: string;
}
const agentInstructions = (runContext: RunContext<AgentContext>, _agent: Agent<AgentContext>) => {
  const { inputOutputParsedUrl, inputOutputParsedSummary } = runContext.context;
  return `From this site:  ${inputOutputParsedUrl} here's a summary about data:  ${inputOutputParsedSummary}
Show this information to the user. Do not include any technical information (how data was selected, which parameters and values was used, etc.)`
}
const agent7 = new Agent({
  name: "Agent",
  instructions: agentInstructions,
  model: "gpt-5.1",
  outputType: AgentSchema3,
  modelSettings: {
    reasoning: {
      effort: "low",
      summary: "auto"
    },
    store: true
  }
});

const approvalRequest = (message: string) => {

  // TODO: Implement
  return true;
}

type WorkflowInput = { input_as_text: string };


// Main code entrypoint
export const runWorkflow = async (workflow: WorkflowInput) => {
  return await withTrace("ScF_webpage_assistant_v1", async () => {
    const state = {

    };
    const conversationHistory: AgentInputItem[] = [
      { role: "user", content: [{ type: "input_text", text: workflow.input_as_text }] }
    ];
    const runner = new Runner({
      traceMetadata: {
        __trace_source__: "agent-builder",
        workflow_id: "wf_690fa71c4f0081908ea1b2e418a985cf0f8f7106641c68d8"
      }
    });
    const guardrailsInputText = workflow.input_as_text;
    const { hasTripwire: guardrailsHasTripwire, safeText: guardrailsAnonymizedText, failOutput: guardrailsFailOutput, passOutput: guardrailsPassOutput } = await runAndApplyGuardrails(guardrailsInputText, guardrailsConfig, conversationHistory, workflow);
    const guardrailsOutput = (guardrailsHasTripwire ? guardrailsFailOutput : guardrailsPassOutput);
    if (guardrailsHasTripwire) {
      return guardrailsOutput;
    } else {
      const myAgentResultTemp = await runner.run(
        myAgent,
        [
          ...conversationHistory
        ]
      );
      conversationHistory.push(...myAgentResultTemp.newItems.map((item) => item.rawItem));

      if (!myAgentResultTemp.finalOutput) {
          throw new Error("Agent result is undefined");
      }

      const myAgentResult = {
        output_text: JSON.stringify(myAgentResultTemp.finalOutput),
        output_parsed: myAgentResultTemp.finalOutput
      };
      if (myAgentResult.output_parsed.topic == "career") {
        const agentResultTemp = await runner.run(
          agent,
          [
            ...conversationHistory
          ]
        );
        conversationHistory.push(...agentResultTemp.newItems.map((item) => item.rawItem));

        if (!agentResultTemp.finalOutput) {
            throw new Error("Agent result is undefined");
        }

        const agentResult = {
          output_text: agentResultTemp.finalOutput ?? ""
        };
        const agentResultTemp1 = await runner.run(
          agent5,
          [
            ...conversationHistory
          ]
        );
        conversationHistory.push(...agentResultTemp1.newItems.map((item) => item.rawItem));

        if (!agentResultTemp1.finalOutput) {
            throw new Error("Agent result is undefined");
        }

        const agentResult1 = {
          output_text: JSON.stringify(agentResultTemp1.finalOutput),
          output_parsed: agentResultTemp1.finalOutput
        };
        if (agentResult1.output_parsed.interest) {
          const approvalMessage = "Prepare e-mail to the position?";

          if (approvalRequest(approvalMessage)) {
              const agentResultTemp2 = await runner.run(
                agent4,
                [
                  ...conversationHistory
                ]
              );
              conversationHistory.push(...agentResultTemp2.newItems.map((item) => item.rawItem));

              if (!agentResultTemp2.finalOutput) {
                  throw new Error("Agent result is undefined");
              }

              const agentResult2 = {
                output_text: JSON.stringify(agentResultTemp2.finalOutput),
                output_parsed: agentResultTemp2.finalOutput
              };
          } else {
              return agentResult1;
          }
        } else {

        }
      } else if (myAgentResult.output_parsed.topic == "blog" || myAgentResult.output_parsed.topic == "case studies") {
        const agentResultTemp = await runner.run(
          agent6,
          [
            ...conversationHistory
          ]
        );
        conversationHistory.push(...agentResultTemp.newItems.map((item) => item.rawItem));

        if (!agentResultTemp.finalOutput) {
            throw new Error("Agent result is undefined");
        }

        const agentResult = {
          output_text: JSON.stringify(agentResultTemp.finalOutput),
          output_parsed: agentResultTemp.finalOutput
        };
        const agentResultTemp1 = await runner.run(
          agent7,
          [
            ...conversationHistory
          ],
          {
            context: {
              inputOutputParsedUrl: agentResult.output_parsed.url,
              inputOutputParsedSummary: agentResult.output_parsed.summary
            }
          }
        );
        conversationHistory.push(...agentResultTemp1.newItems.map((item) => item.rawItem));

        if (!agentResultTemp1.finalOutput) {
            throw new Error("Agent result is undefined");
        }

        const agentResult1 = {
          output_text: JSON.stringify(agentResultTemp1.finalOutput),
          output_parsed: agentResultTemp1.finalOutput
        };
        return agentResult1;
      } else if (myAgentResult.output_parsed.topic == "contacts") {
        const agentResultTemp = await runner.run(
          agent2,
          [
            ...conversationHistory
          ]
        );
        conversationHistory.push(...agentResultTemp.newItems.map((item) => item.rawItem));

        if (!agentResultTemp.finalOutput) {
            throw new Error("Agent result is undefined");
        }

        const agentResult = {
          output_text: agentResultTemp.finalOutput ?? ""
        };
      } else if (myAgentResult.output_parsed.topic == "technology stack") {
        const agentResultTemp = await runner.run(
          agent3,
          [
            ...conversationHistory
          ]
        );
        conversationHistory.push(...agentResultTemp.newItems.map((item) => item.rawItem));

        if (!agentResultTemp.finalOutput) {
            throw new Error("Agent result is undefined");
        }

        const agentResult = {
          output_text: agentResultTemp.finalOutput ?? ""
        };
      } else {
        const agentResultTemp = await runner.run(
          agent1,
          [
            ...conversationHistory
          ]
        );
        conversationHistory.push(...agentResultTemp.newItems.map((item) => item.rawItem));

        if (!agentResultTemp.finalOutput) {
            throw new Error("Agent result is undefined");
        }

        const agentResult = {
          output_text: agentResultTemp.finalOutput ?? ""
        };
      }
    }
  });
}
