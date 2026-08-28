import React, { useState, useEffect, useRef } from 'react';

declare const acquireVsCodeApi: any;

// Safely acquire the API only if it hasn't been acquired yet
const vscode = (window as any).vscodeApi || ((window as any).vscodeApi = acquireVsCodeApi());

// 1. Fetch the saved state BEFORE initializing the component
const previousState = vscode.getState() || { 
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

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', padding: '10px', boxSizing: 'border-box' }}>
            <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '10px', paddingBottom: '10px' }}>
                {messages.length === 0 && (
                    <div style={{ opacity: 0.5, textAlign: 'center', marginTop: '2rem' }}>
                        How can I help you today?
                    </div>
                )}
                {messages.map((msg, i) => (
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
                ))}
                
                {isLoading && currentThought && (
                    <div style={{ alignSelf: 'flex-start', opacity: 0.7, fontStyle: 'italic', padding: '8px 12px' }}>
                        <span className="spinner">🌀</span> {currentThought}
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
