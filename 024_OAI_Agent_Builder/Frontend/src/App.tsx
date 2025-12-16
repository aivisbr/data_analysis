import { Hero } from './components/Hero';
import { Features } from './components/Features';
import { About } from './components/About';
import { CTA } from './components/CTA';
import { Footer } from './components/Footer';
import { ChatButton } from './components/ChatButton';

function App() {
  return (
    <div className="min-h-screen">
      <Hero />
      <Features />
      <About />
      <CTA />
      <Footer />
      <ChatButton />
    </div>
  );
}

export default App;
