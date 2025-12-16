import { ArrowRight, Sparkles } from 'lucide-react';

export function Hero() {
  return (
    <section className="relative min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 text-white overflow-hidden">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,rgba(56,189,248,0.1),transparent_50%)]"></div>
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_70%_80%,rgba(14,165,233,0.1),transparent_50%)]"></div>

      <div className="relative max-w-7xl mx-auto px-6 py-24 text-center z-10">
        <div className="inline-flex items-center gap-2 bg-sky-500/10 border border-sky-500/20 rounded-full px-4 py-2 mb-8 backdrop-blur-sm">
          <Sparkles className="w-4 h-4 text-sky-400" />
          <span className="text-sm font-medium text-sky-300">Innovation at the Core</span>
        </div>

        <h1 className="text-5xl md:text-7xl font-bold mb-6 bg-clip-text text-transparent bg-gradient-to-r from-white via-sky-200 to-white leading-tight">
          Scandic Fusion
        </h1>

        <p className="text-xl md:text-2xl text-slate-300 mb-4 max-w-3xl mx-auto leading-relaxed">
          Transforming Tomorrow with Intelligent Technology
        </p>

        <p className="text-lg text-slate-400 mb-12 max-w-2xl mx-auto leading-relaxed">
          We build cutting-edge solutions that bridge the gap between innovation and implementation,
          empowering businesses to thrive in the digital age.
        </p>

        <div className="flex flex-col sm:flex-row gap-4 justify-center items-center">
          <button className="group px-8 py-4 bg-sky-500 hover:bg-sky-600 text-white rounded-lg font-semibold transition-all duration-300 flex items-center gap-2 shadow-lg hover:shadow-sky-500/25 hover:scale-105">
            Get Started
            <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
          </button>
          <button className="px-8 py-4 bg-white/5 hover:bg-white/10 text-white rounded-lg font-semibold transition-all duration-300 backdrop-blur-sm border border-white/10 hover:border-white/20">
            Learn More
          </button>
        </div>
      </div>

      <div className="absolute bottom-0 left-0 right-0 h-32 bg-gradient-to-t from-white to-transparent"></div>
    </section>
  );
}
