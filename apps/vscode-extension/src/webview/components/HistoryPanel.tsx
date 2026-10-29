import React from 'react';

export interface ChatSession {
    id: string;
    title: string;
    timestamp: number;
}

interface HistoryPanelProps {
    sessions: ChatSession[];
    onSelectSession: (session: ChatSession) => void;
    onDeleteSession: (sessionId: string, e: React.MouseEvent) => void;
    onSearchQuery: (query: string) => void;
    searchQuery: string;
    onClose: () => void;
}

export const HistoryPanel: React.FC<HistoryPanelProps> = ({
    sessions,
    onSelectSession,
    onDeleteSession,
    onSearchQuery,
    searchQuery,
    onClose
}) => {
    return (
        <div style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, background: 'var(--vscode-editor-background)', zIndex: 10, padding: '10px', display: 'flex', flexDirection: 'column' }}>
            <button onClick={onClose} style={{ alignSelf: 'flex-end' }}>❌ Close</button>
            <input 
                type="text" 
                placeholder="Search history..." 
                value={searchQuery} 
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => onSearchQuery(e.target.value)} 
                style={{ margin: '10px 0', padding: '5px' }}
            />
            <div style={{ overflowY: 'auto', flex: 1 }}>
                {sessions.map((s: ChatSession) => (
                    <div 
                        key={s.id} 
                        onClick={() => onSelectSession(s)} 
                        style={{ padding: '10px', borderBottom: '1px solid gray', cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}
                    >
                        <div>
                            <strong>{s.title}</strong>
                            <div style={{ fontSize: '10px', opacity: 0.7 }}>{new Date(s.timestamp).toLocaleString()}</div>
                        </div>
                        <button 
                            title="Delete session"
                            onClick={(e: React.MouseEvent<HTMLButtonElement>) => onDeleteSession(s.id, e)}
                            style={{ background: 'transparent', border: 'none', cursor: 'pointer', fontSize: '14px' }}
                        >
                            🗑️
                        </button>
                    </div>
                ))}
            </div>
        </div>
    );
};