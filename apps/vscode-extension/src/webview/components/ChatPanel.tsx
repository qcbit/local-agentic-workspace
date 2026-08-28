import React, { useState, useEffect, useRef } from 'react';

declare const acquireVsCodeApi: any;

// Safely acquire the API only if it hasn't been acquired yet
const vscode = (window as any).vscodeApi || ((window as any).vscodeApi = acquireVsCodeApi());

type ChatSession = {
    id: string;
    title: string;
    messages: { role: string; content: string }[];
    timestamp: number;
};

// Update previousState extraction
const previousState = vscode.getState() || { 
    sessions: [],
    currentSessionId: Date.now().toString(),
    messages: [], 
    input: '', 
    isAutoApprove: false,
    isLoading: false
};

export const ChatPanel: React.FC = () => {
    // 2. Initialize your React state using the saved values
    const [messages, setMessages] = useState<{ role: string, content: string }[]>(previousState.messages);
    const [input, setInput] = useState<string>(previousState.input);
    const [isAutoApprove, setIsAutoApprove] = useState<boolean>(previousState.isAutoApprove);
    const [isLoading, setIsLoading] = useState<boolean>(false); // Always start unlocked, ignoring previousState.isLoading
    const [currentThought, setCurrentThought] = useState<string>("");
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const [sessions, setSessions] = useState<ChatSession[]>(previousState.sessions || []);
    const [currentSessionId, setCurrentSessionId] = useState<string>(previousState.currentSessionId || Date.now().toString());
    const [isHistoryOpen, setIsHistoryOpen] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");

    // 3. Save to VS Code's internal state manager whenever these values change
    useEffect(() => {
        vscode.setState({
            messages,
            input,
            isAutoApprove,
            isLoading
        });
    }, [messages, input, isAutoApprove, isLoading]);

    useEffect(() => {
        const handler = (event: MessageEvent) => {
            const message = event.data;
            if (message.command === 'agentThinking') {
                setMessages(prev => [...prev, { role: 'thought', content: message.text }]);
                setCurrentThought(message.text);
            } 
            // Intercept proactive terminal errors and render them as user prompts
            else if (message.command === 'injectMessage') {
                setMessages(prev => [...prev, { role: 'user', content: message.text }]);
                setIsLoading(true);
                setCurrentThought("Initializing...");
            }
            else if (message.command === 'agentResponse') {
                setMessages(prev => [...prev, { role: 'agent', content: message.text }]);
                setIsLoading(false);
                setCurrentThought(""); 
            } 
            else if (message.command === 'agentError') {
                setMessages(prev => [...prev, { role: 'error', content: message.text }]);
                setIsLoading(false);
                setCurrentThought(""); 
            }
            // 🛑 Reset UI if the agent was forcefully stopped
            else if (message.type === 'agent_stopped') {
                setMessages(prev => [...prev, { role: 'error', content: 'Agent task was manually canceled.' }]);
                setIsLoading(false);
                setCurrentThought("");
            }
        };
        window.addEventListener('message', handler);
        return () => window.removeEventListener('message', handler);
    }, []);

    const saveCurrentSession = () => {
        if (messages.length === 0) return;
        const title = messages.find(m => m.role === 'user')?.content.substring(0, 30) || "New Chat";
        setSessions(prev => {
            const existing = prev.filter(s => s.id !== currentSessionId);
            return [{ id: currentSessionId, title, messages, timestamp: Date.now() }, ...existing];
        });
    };

    const handleNewChat = () => {
        saveCurrentSession();
        setMessages([]);
        setCurrentSessionId(Date.now().toString());
        setIsHistoryOpen(false);
        vscode.postMessage({ type: 'reset_session' });
    };

    const loadSession = (session: ChatSession) => {
        saveCurrentSession();
        setMessages(session.messages);
        setCurrentSessionId(session.id);
        setIsHistoryOpen(false);
        // Send history to Python to rebuild AgentState
        vscode.postMessage({ type: 'restore_session', messages: session.messages });
    };
    const handleSend = () => {
        if (!input.trim() || isLoading) return;
        
        setMessages(prev => [...prev, { role: 'user', content: input }]);
        setIsLoading(true);
        setCurrentThought("Initializing..."); 
        
        vscode.postMessage({
            command: 'executeTask',
            goal: input,
            autoApprove: isAutoApprove
        });
        
        setInput('');
        if (textareaRef.current) {
            textareaRef.current.style.height = 'auto';
        }
    };

    const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        setInput(e.target.value);
        if (textareaRef.current) {
            textareaRef.current.style.height = 'auto';
            const newHeight = Math.min(textareaRef.current.scrollHeight, 200);
            textareaRef.current.style.height = `${newHeight}px`;
            textareaRef.current.style.overflowY = textareaRef.current.scrollHeight > 200 ? 'auto' : 'hidden';
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault(); 
            handleSend();
        }
    };

    // 🎯 The function that fires the stop signal to the extension host
    const handleStopAgent = () => {
        // 1. Fire the signal to the backend
        vscode.postMessage({
            type: 'stop_agent'
        });
        // 2. INSTANTLY forcefully unlock the UI (Break the deadlock!)
        setIsLoading(false);
        setCurrentThought("");
        setMessages(prev => [...prev, { role: 'error', content: '🛑 Agent task forcefully aborted.' }]);
    };

    const filteredSessions = sessions.filter(s => 
        s.title.toLowerCase().includes(searchQuery.toLowerCase()) || 
        s.messages.some(m => m.content.toLowerCase().includes(searchQuery.toLowerCase()))
    );

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', padding: '10px', boxSizing: 'border-box' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', paddingBottom: '10px' }}>
                <button onClick={() => setIsHistoryOpen(!isHistoryOpen)}>📜 History</button>
                <button onClick={handleNewChat}>➕ New Chat</button>
            </div>

            {isHistoryOpen && (
                <div style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, background: 'var(--vscode-editor-background)', zIndex: 10, padding: '10px', display: 'flex', flexDirection: 'column' }}>
                    <button onClick={() => setIsHistoryOpen(false)} style={{ alignSelf: 'flex-end' }}>❌ Close</button>
                    <input 
                        type="text" 
                        placeholder="Search history..." 
                        value={searchQuery} 
                        onChange={(e) => setSearchQuery(e.target.value)} 
                        style={{ margin: '10px 0', padding: '5px' }}
                    />
                    <div style={{ overflowY: 'auto', flex: 1 }}>
                        {filteredSessions.map(s => (
                            <div key={s.id} onClick={() => loadSession(s)} style={{ padding: '10px', borderBottom: '1px solid gray', cursor: 'pointer' }}>
                                <strong>{s.title}</strong>
                                <div style={{ fontSize: '10px', opacity: 0.7 }}>{new Date(s.timestamp).toLocaleString()}</div>
                            </div>
                        ))}
                    </div>
                </div>
            )}
            <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '10px', paddingBottom: '10px' }}>
                {messages.length === 0 && (
                    <div style={{ opacity: 0.5, textAlign: 'center', marginTop: '2rem' }}>
                        How can I help you today?
                    </div>
                )}
                {messages.map((msg, i) => (
                    msg.role === 'thought' ? (
                        // 🎯 Distinct styling for intermediate reasoning steps
                        <div key={i} style={{
                            alignSelf: 'flex-start', 
                            opacity: 0.7, 
                            fontStyle: 'italic', 
                            padding: '4px 12px',
                            fontSize: '0.9em',
                            whiteSpace: 'pre-wrap',
                            wordWrap: 'break-word'
                        }}>
                            {msg.content}
                        </div>
                    ) : (
                        // Standard styling for user, agent, and error messages
                        <div key={i} style={{
                            padding: '8px 12px', 
                            borderRadius: '6px',
                            backgroundColor: msg.role === 'user' ? 'var(--vscode-button-background)' : 'var(--vscode-editor-inactiveSelectionBackground)',
                            color: msg.role === 'user' ? 'var(--vscode-button-foreground)' : 'var(--vscode-editor-foreground)',
                            alignSelf: msg.role === 'user' ? 'flex-end' : 'flex-start',
                            maxWidth: '90%',
                            whiteSpace: 'pre-wrap',
                            wordWrap: 'break-word'
                        }}>
                            {msg.role === 'error' ? '❌ ' : ''}{msg.content}
                        </div>
                    )
                ))}
                
                {isLoading && (
                    <div style={{ alignSelf: 'flex-start', opacity: 0.7, fontStyle: 'italic', padding: '8px 12px' }}>
                        <span className="spinner">⚙️</span> {currentThought || "Thinking..."}
                    </div>
                )}
            </div>
            
            <div style={{ display: 'flex', gap: '8px', paddingTop: '10px', borderTop: '1px solid var(--vscode-widget-border)', alignItems: 'flex-end' }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: '4px', color: 'var(--vscode-foreground)', fontSize: '12px', cursor: 'pointer', paddingBottom: '8px' }}>
                    <input 
                        type="checkbox" 
                        checked={isAutoApprove}
                        onChange={(e) => setIsAutoApprove(e.target.checked)}
                        style={{ margin: 0 }}
                    />
                    Auto-Approve
                </label>
            </div>
                
            <div style={{ display: 'flex', gap: '8px', paddingTop: '10px', alignItems: 'flex-end' }}>
                {/* Multiline input */}
                <textarea 
                    ref={textareaRef}
                    value={input}
                    onChange={handleInputChange}
                    onKeyDown={handleKeyDown}
                    placeholder="Ask the agent... (Shift+Enter for new line)" 
                    rows={1}
                    style={{ 
                        flex: 1, 
                        padding: '8px', 
                        backgroundColor: 'var(--vscode-input-background)', 
                        color: 'var(--vscode-input-foreground)', 
                        border: '1px solid var(--vscode-input-border)', 
                        outline: 'none',
                        resize: 'none',
                        overflowY: 'hidden',
                        fontFamily: 'inherit',
                        minHeight: '18px',
                        maxHeight: '200px',
                        borderRadius: '2px'
                    }}
                />
                
                {/* 🎯 Toggle between Send and Stop based on isLoading */}
                {isLoading ? (
                    <button 
                        onClick={handleStopAgent}
                        style={{ padding: '8px 12px', backgroundColor: '#d32f2f', color: 'white', border: 'none', cursor: 'pointer', marginBottom: '2px', borderRadius: '2px' }}
                    >
                        🛑 Stop
                    </button>
                ) : (
                    <button 
                        onClick={handleSend}
                        disabled={!input.trim()}
                        style={{ padding: '8px 12px', backgroundColor: 'var(--vscode-button-background)', color: 'var(--vscode-button-foreground)', border: 'none', cursor: !input.trim() ? 'not-allowed' : 'pointer', marginBottom: '2px', borderRadius: '2px' }}
                    >
                        Send
                    </button>
                )}
            </div>
        </div>
    );
};
