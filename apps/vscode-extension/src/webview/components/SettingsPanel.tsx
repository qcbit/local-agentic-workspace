import React, { useState, useEffect } from 'react';

// Safely acquire and cache the VS Code API
declare const acquireVsCodeApi: any;
const vscode = (window as any).vscodeApi || ((window as any).vscodeApi = acquireVsCodeApi());

export const SettingsPanel: React.FC = () => {
    const [isLoading, setIsLoading] = useState(true);
    const [rawConfig, setRawConfig] = useState<any>(null);
    
    // Profile & LLM Settings
    const [activeProfile, setActiveProfile] = useState('home');
    const [endpointUrl, setEndpointUrl] = useState('');
    const [apiKey, setApiKey] = useState('');
    const [modelName, setModelName] = useState(''); 
    const [tokenLimit, setTokenLimit] = useState(6000);
    const [availableModels, setAvailableModels] = useState<string[]>([]);

    // Helper to populate form fields from a specific profile object
    const applyProfileState = (profileName: string, config: any) => {
        const profile = config?.profiles?.[profileName];
        if (profile) {
            setEndpointUrl(profile.llm?.endpoint_url || profile.llm?.endpoint || '');
            setApiKey(profile.llm?.api_key || profile.llm?.apiKey || '');
            setModelName(profile.llm?.model_name || profile.llm?.model || '');
            setTokenLimit(profile.memory?.max_tokens || 6000);
        } else {
            // Sensible fallbacks if profile entry does not exist yet
            if (profileName === 'work') {
                setEndpointUrl('https://your-enterprise-endpoint.azure.com/v1/chat/completions');
                setApiKey('');
                setModelName('gpt-4-turbo');
                setTokenLimit(8000);
            } else {
                setEndpointUrl('http://127.0.0.1:11434/v1/chat/completions');
                setApiKey('none');
                setModelName('llama3:8b');
                setTokenLimit(6000);
            }
        }
    };

    useEffect(() => {
        vscode.postMessage({ command: 'ready' });

        const handleMessage = (event: MessageEvent) => {
            const message = event.data;
            
            if (message.command === 'loadModels') {
                setAvailableModels(message.models || []);
            } 
            else if (message.command === 'loadConfig') {
                const config = message.config;
                if (config) {
                    setRawConfig(config);
                    const currentProfile = config.active_profile || 'home';
                    setActiveProfile(currentProfile);
                    applyProfileState(currentProfile, config);
                }
                setIsLoading(false);
            }
        };

        window.addEventListener('message', handleMessage);
        return () => window.removeEventListener('message', handleMessage);
    }, []);

    // Switch profile inputs dynamically when changing the dropdown
    const handleProfileChange = (newProfile: string) => {
        setActiveProfile(newProfile);
        if (rawConfig) {
            applyProfileState(newProfile, rawConfig);
        }
    };

    const handleSync = () => {
        vscode.postMessage({
            command: 'updateSetting',
            config: {
                active_profile: activeProfile,
                profile_settings: {
                    llm: { 
                        endpoint_url: endpointUrl,
                        api_key: apiKey,
                        model_name: modelName 
                    },
                    memory: { 
                        max_tokens: tokenLimit 
                    }
                }
            }
        });
    };

    if (isLoading) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100vh', gap: '1rem', color: 'var(--vscode-foreground)' }}>
                <div style={{ 
                    border: '4px solid var(--vscode-widget-border)', 
                    borderTop: '4px solid var(--vscode-button-background)', 
                    borderRadius: '50%', 
                    width: '40px', 
                    height: '40px', 
                    animation: 'spin 1s linear infinite' 
                }}></div>
                <style>{`
                    @keyframes spin {
                        0% { transform: rotate(0deg); }
                        100% { transform: rotate(360deg); }
                    }
                `}</style>
                <h3>Fetching Workspace Environment...</h3>
            </div>
        );
    }

    const inputStyle: React.CSSProperties = {
        padding: '0.5rem',
        marginTop: '0.25rem',
        backgroundColor: 'var(--vscode-input-background)',
        color: 'var(--vscode-input-foreground)',
        border: '1px solid var(--vscode-input-border)'
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', padding: '1rem' }}>
            <h2>Workspace Environment</h2>
            
            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Active Profile</label>
                <select 
                    value={activeProfile} 
                    onChange={(e) => handleProfileChange(e.target.value)}
                    style={inputStyle}
                >
                    <option value="home">🏠 Home (Local Ollama)</option>
                    <option value="work">🏢 Work (Azure Foundry / Cloud)</option>
                </select>
            </div>

            <hr style={{ width: '100%', borderColor: 'var(--vscode-widget-border)' }} />
            
            <h3>{activeProfile.toUpperCase()} Profile Settings</h3>

            {/* Endpoint URL Field */}
            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Endpoint URL</label>
                <input 
                    type="text"
                    value={endpointUrl}
                    onChange={(e) => setEndpointUrl(e.target.value)}
                    placeholder="e.g. https://<instance>.openai.azure.com/openai/deployments/<model>/chat/completions?api-version=2024-02-15-preview"
                    style={inputStyle}
                />
            </div>

            {/* API Key Field */}
            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>API Key / Auth Token</label>
                <input 
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={activeProfile === 'home' ? 'none (Ollama)' : 'Enter your Azure / OpenAI API key'}
                    style={inputStyle}
                />
            </div>

            {/* Model Name Field (Supports typing or selecting from detected models) */}
            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Model Deployment Name</label>
                <input 
                    type="text"
                    list="available-models-list"
                    value={modelName}
                    onChange={(e) => setModelName(e.target.value)}
                    placeholder="e.g. gpt-4-turbo, qwen2.5-coder, or llama3:8b"
                    style={inputStyle}
                />
                <datalist id="available-models-list">
                    {availableModels.map(model => (
                        <option key={model} value={model} />
                    ))}
                </datalist>
            </div>

            {/* Token Limit Field */}
            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Max Context Tokens</label>
                <input 
                    type="number" 
                    value={tokenLimit}
                    onChange={(e) => setTokenLimit(Number(e.target.value))}
                    style={inputStyle}
                />
            </div>

            <button 
                onClick={handleSync}
                style={{ 
                    padding: '0.6rem 1rem', 
                    cursor: 'pointer', 
                    marginTop: '0.5rem', 
                    backgroundColor: 'var(--vscode-button-background)', 
                    color: 'var(--vscode-button-foreground)', 
                    border: 'none',
                    fontWeight: 'bold'
                }}
            >
                Save & Sync to Orchestrator
            </button>
        </div>
    );
};
