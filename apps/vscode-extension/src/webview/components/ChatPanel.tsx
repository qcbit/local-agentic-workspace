import React, { useState, useEffect } from 'react';

declare const acquireVsCodeApi: any;

// Safely acquire the API only if it hasn't been acquired yet
const vscode = (window as any).vscodeApi || ((window as any).vscodeApi = acquireVsCodeApi());

export const ChatPanel: React.FC = () => {
    const [input, setInput] = useState('');
    const [messages, setMessages] = useState<{ role: string, content: string }[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isAutoApprove, setIsAutoApprove] = useState(false);

    // Listen for IPC responses routed from the Python orchestrator
    useEffect(() => {
        const handler = (event: MessageEvent) => {
            const message = event.data;
            if (message.command === 'agentResponse') {
                setMessages(prev => [...prev, { role: 'agent', content: message.text }]);
                setIsLoading(false);
            } else if (message.command === 'agentError') {
                setMessages(prev => [...prev, { role: 'error', content: message.text }]);
                setIsLoading(false);
            }
        };
        window.addEventListener('message', handler);
        return () => window.removeEventListener('message', handler);
    }, []);

    const handleSend = () => {
        if (!input.trim() || isLoading) return;
        
        // Render user message immediately
        setMessages(prev => [...prev, { role: 'user', content: input }]);
        setIsLoading(true);
        
        // Dispatch command to the Extension Host
        vscode.postMessage({
            command: 'executeTask',
            goal: input,
            autoApprove: isAutoApprove
        });
        
        setInput('');
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
                {isLoading && <div style={{ alignSelf: 'flex-start', opacity: 0.7, fontStyle: 'italic' }}>Agent is reasoning...</div>}
            </div>
            
            <div style={{ display: 'flex', gap: '8px', paddingTop: '10px', borderTop: '1px solid var(--vscode-widget-border)' }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: '4px', color: 'var(--vscode-foreground)', fontSize: '12px', cursor: 'pointer' }}>
                    <input 
                        type="checkbox" 
                        checked={isAutoApprove}
                        onChange={(e) => setIsAutoApprove(e.target.checked)}
                        style={{ margin: 0 }}
                    />
                    Auto-Approve
                </label>
                <input 
                    type="text" 
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleSend()}
                    placeholder="Ask the agent..."
                    style={{ flex: 1, padding: '8px', backgroundColor: 'var(--vscode-input-background)', color: 'var(--vscode-input-foreground)', border: '1px solid var(--vscode-input-border)', outline: 'none' }}
                />
                <button 
                    onClick={handleSend}
                    disabled={isLoading}
                    style={{ padding: '8px 12px', backgroundColor: 'var(--vscode-button-background)', color: 'var(--vscode-button-foreground)', border: 'none', cursor: isLoading ? 'not-allowed' : 'pointer' }}
                >
                    Send
                </button>
            </div>
        </div>
    );
};
