import { useState } from 'react';

function SimpleApp() {
  const [count, setCount] = useState(0);
  
  return (
    <div style={{ padding: '20px', fontFamily: 'Arial' }}>
      <h1>✅ React is Working!</h1>
      <p>If you see this, React is rendering correctly.</p>
      <button onClick={() => setCount(count + 1)}>
        Count: {count}
      </button>
      <hr />
      <p>Backend: {window.location.protocol}//{window.location.hostname}:8000</p>
      <p>Frontend: {window.location.href}</p>
    </div>
  );
}

export default SimpleApp;

