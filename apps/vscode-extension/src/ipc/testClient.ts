import { UdsClient } from './UdsClient';

async function runTest() {
    const client = new UdsClient();
    
    try {
        console.log('🔄 Attempting to connect...');
        await client.connect();
        
        // Test 1: Fast Ping
        console.log('\n📤 Sending ping...');
        const pingResult = await client.request('ping');
        console.log('📥 Ping result:', pingResult);
        
        // Test 2: Agent Execution
        console.log('\n📤 Sending task to Agent...');
        const agentResult = await client.request('execute_agent_task', {
            goal: 'List the files in my current directory.'
        });
        
        console.log('\n📥 Final Agent Response:');
        console.dir(agentResult, { depth: null, colors: true });
        
    } catch (error) {
        console.error('\n❌ Test failed:', error);
    } finally {
        // Exit the Node process once the promises resolve
        process.exit(0);
    }
}

runTest();
