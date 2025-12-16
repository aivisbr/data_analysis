import { ChatKit, useChatKit } from '@openai/chatkit-react';

export function ChatWidget() {
  const { control } = useChatKit({
    api: {
      async getClientSecret(existing) {
        if (existing) {
          return existing;
        }

        const res = await fetch('https://38a30bf4d507.ngrok-free.app/api/chatkit/session', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
        });
        const { client_secret } = await res.json();
        return client_secret;
      },
    },
  });

  return (
    <div className="h-[600px] w-[380px] shadow-2xl rounded-2xl overflow-hidden border border-slate-200">
      <ChatKit control={control} />
    </div>
  );
}
