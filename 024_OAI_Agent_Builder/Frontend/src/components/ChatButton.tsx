import { useState } from 'react';
import { MessageCircle, X } from 'lucide-react';
import { ChatWidget } from './ChatWidget';

export function ChatButton() {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <>
      <div
        className={`fixed bottom-6 left-6 z-50 transition-all duration-300 ${
          isOpen ? 'opacity-100 scale-100' : 'opacity-0 scale-0 pointer-events-none'
        }`}
      >
        <ChatWidget />
      </div>

      <button
        onClick={() => setIsOpen(!isOpen)}
        className="fixed bottom-6 left-6 z-50 w-16 h-16 bg-sky-500 hover:bg-sky-600 text-white rounded-full shadow-2xl hover:shadow-sky-500/50 transition-all duration-300 flex items-center justify-center group hover:scale-110"
        aria-label={isOpen ? 'Close chat' : 'Open chat'}
      >
        {isOpen ? (
          <X className="w-7 h-7 transition-transform group-hover:rotate-90 duration-300" />
        ) : (
          <MessageCircle className="w-7 h-7 transition-transform group-hover:scale-110 duration-300" />
        )}
      </button>
    </>
  );
}
