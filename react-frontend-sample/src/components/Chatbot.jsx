import { useState, useRef, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import './Chatbot.css';

const API_BASE_URL = 'http://localhost:8000';

export default function Chatbot() {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState(() => {
    // Get session from localStorage or create new one
    return localStorage.getItem('chatbot_session_id') || '';
  });
  
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Focus input when chat opens
  useEffect(() => {
    if (isOpen) {
      inputRef.current?.focus();
    }
  }, [isOpen]);

  // Load chat history when opening
  useEffect(() => {
    if (isOpen && sessionId) {
      loadChatHistory();
    }
  }, [isOpen]);

  const loadChatHistory = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/api/chat/history/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId }),
      });
      
      if (response.ok) {
        const data = await response.json();
        if (data.messages && data.messages.length > 0) {
          setMessages(
            data.messages
              .filter(msg => msg.role !== 'system')
              .map(msg => ({
                role: msg.role,
                content: msg.content,
                timestamp: new Date(msg.timestamp),
              }))
          );
        }
      }
    } catch (error) {
      console.error('Failed to load chat history:', error);
    }
  };

  const sendMessage = async (e) => {
    e.preventDefault();
    
    if (!inputMessage.trim() || isLoading) return;

    const userMessage = {
      role: 'user',
      content: inputMessage,
      timestamp: new Date(),
    };

    setMessages(prev => [...prev, userMessage]);
    setInputMessage('');
    setIsLoading(true);

    try {
      const response = await fetch(`${API_BASE_URL}/api/chat/send-stream/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: inputMessage,
          session_id: sessionId,
        }),
      });

      if (!response.ok) {
        throw new Error('Failed to send message');
      }

      // Handle streaming response
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let assistantMessage = {
        role: 'assistant',
        content: '',
        timestamp: new Date(),
      };
      
      // Add empty assistant message to show typing
      setMessages(prev => [...prev, assistantMessage]);
      let messageIndex = messages.length + 1;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        
        // Process line-by-line
        let newlineIndex;
        while ((newlineIndex = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, newlineIndex).trim();
          buffer = buffer.slice(newlineIndex + 1);
          
          if (!line) continue;

          try {
            const event = JSON.parse(line);
            
            if (event.type === 'session') {
              const newSessionId = event.session_id;
              setSessionId(newSessionId);
              localStorage.setItem('chatbot_session_id', newSessionId);
            } else if (event.type === 'chunk') {
              // Update the assistant message content
              setMessages(prev => {
                const updated = [...prev];
                updated[messageIndex] = {
                  ...updated[messageIndex],
                  content: updated[messageIndex].content + event.content,
                };
                return updated;
              });
            } else if (event.type === 'done') {
              setIsLoading(false);
            }
          } catch (err) {
            console.error('Failed to parse event:', err);
          }
        }
      }
    } catch (error) {
      console.error('Error sending message:', error);
      setMessages(prev => [
        ...prev,
        {
          role: 'assistant',
          content: '⚠️ Sorry, I encountered an error. Please try again.',
          timestamp: new Date(),
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  const clearHistory = async (shouldConfirm = true) => {
    if (shouldConfirm && !confirm('Are you sure you want to clear the chat history?')) return;

    try {
      await fetch(`${API_BASE_URL}/api/chat/clear/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId }),
      });
      
      setMessages([]);
      const newSessionId = '';
      setSessionId(newSessionId);
      localStorage.removeItem('chatbot_session_id');
    } catch (error) {
      console.error('Failed to clear history:', error);
    }
  };

  const handleToggle = () => {
    if (isOpen) {
      clearHistory(false);
    }
    setIsOpen(!isOpen);
  };

  return (
    <>
      {/* Chat Button */}
      <motion.button
        className="chatbot-button"
        onClick={handleToggle}
        whileHover={{ scale: 1.1 }}
        whileTap={{ scale: 0.9 }}
        animate={{ rotate: isOpen ? 180 : 0 }}
      >
        {isOpen ? '✕' : '💬'}
      </motion.button>

      {/* Chat Window */}
      <AnimatePresence>
        {isOpen && (
          <motion.div
            className="chatbot-window"
            initial={{ opacity: 0, y: 20, scale: 0.9 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.9 }}
            transition={{ duration: 0.2 }}
          >
            {/* Header */}
            <div className="chatbot-header">
              <div className="chatbot-header-content">
                <div className="chatbot-avatar">🤖</div>
                <div>
                  <h3>AI Assistant</h3>
                  <p>Cloud Infrastructure Expert</p>
                </div>
              </div>
              <button className="chatbot-clear-btn" onClick={clearHistory} title="Clear history">
                🗑️
              </button>
            </div>

            {/* Messages */}
            <div className="chatbot-messages">
              {messages.length === 0 && (
                <div className="chatbot-welcome">
                  <h4>👋 Hi! How can I help you today?</h4>
                  <p>Ask me anything about:</p>
                  <ul>
                    <li>AWS & GCP infrastructure</li>
                    <li>Terraform & IaC</li>
                    <li>Container orchestration</li>
                    <li>Cloud migration strategies</li>
                    <li>Security best practices</li>
                  </ul>
                </div>
              )}
              
              {messages.map((msg, idx) => (
                <motion.div
                  key={idx}
                  className={`chatbot-message ${msg.role}`}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.2 }}
                >
                  <div className="message-avatar">
                    {msg.role === 'user' ? '👤' : '🤖'}
                  </div>
                  <div className="message-content">
                    <div className="message-text">{msg.content}</div>
                    <div className="message-time">
                      {msg.timestamp?.toLocaleTimeString([], { 
                        hour: '2-digit', 
                        minute: '2-digit' 
                      })}
                    </div>
                  </div>
                </motion.div>
              ))}
              
              {isLoading && (
                <div className="chatbot-typing">
                  <span></span>
                  <span></span>
                  <span></span>
                </div>
              )}
              
              <div ref={messagesEndRef} />
            </div>

            {/* Input */}
            <form className="chatbot-input-form" onSubmit={sendMessage}>
              <input
                ref={inputRef}
                type="text"
                className="chatbot-input"
                placeholder="Type your message..."
                value={inputMessage}
                onChange={(e) => setInputMessage(e.target.value)}
                disabled={isLoading}
              />
              <button
                type="submit"
                className="chatbot-send-btn"
                disabled={isLoading || !inputMessage.trim()}
              >
                {isLoading ? '⏳' : '➤'}
              </button>
            </form>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

