import { ArrowRight } from 'lucide-react';

export function CTA() {
  return (
    <section className="py-24 bg-white">
      <div className="max-w-5xl mx-auto px-6">
        <div className="bg-gradient-to-br from-sky-500 to-blue-600 rounded-3xl p-12 md:p-16 text-center text-white shadow-2xl relative overflow-hidden">
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_30%_50%,rgba(255,255,255,0.1),transparent_50%)]"></div>

          <div className="relative z-10">
            <h2 className="text-3xl md:text-5xl font-bold mb-6">
              Ready to Transform Your Business?
            </h2>
            <p className="text-xl text-sky-50 mb-10 max-w-2xl mx-auto leading-relaxed">
              Join hundreds of companies that trust Scandic Fusion to power their digital infrastructure.
              Let's build something amazing together.
            </p>
            <div className="flex flex-col sm:flex-row gap-4 justify-center items-center">
              <button className="group px-8 py-4 bg-white text-sky-600 hover:bg-sky-50 rounded-lg font-semibold transition-all duration-300 flex items-center gap-2 shadow-lg hover:shadow-xl hover:scale-105">
                Start Your Journey
                <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
              </button>
              <button className="px-8 py-4 bg-white/10 hover:bg-white/20 text-white rounded-lg font-semibold transition-all duration-300 backdrop-blur-sm border border-white/20 hover:border-white/30">
                Contact Sales
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
