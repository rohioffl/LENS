import { motion } from 'framer-motion';
import Chatbot from './components/Chatbot';

const App = () => {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.5 }}
    >
      <h1>Test App - If you see this, React is working!</h1>
      <p>The chatbot button should appear in the bottom-right corner.</p>
      <Chatbot />
    </motion.div>
  );
};

export default App;

