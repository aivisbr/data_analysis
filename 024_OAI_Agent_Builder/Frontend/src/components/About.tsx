import { CheckCircle2 } from 'lucide-react';

const achievements = [
  'Over 500+ successful implementations',
  '99.9% uptime guarantee',
  'Trusted by Fortune 500 companies',
  '24/7 dedicated support team',
];

export function About() {
  return (
    <section className="py-24 bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 text-white relative overflow-hidden">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_70%_30%,rgba(56,189,248,0.08),transparent_50%)]"></div>

      <div className="max-w-7xl mx-auto px-6 relative z-10">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-16 items-center">
          <div>
            <h2 className="text-4xl md:text-5xl font-bold mb-6 leading-tight">
              Built for the Future of Technology
            </h2>
            <p className="text-lg text-slate-300 mb-8 leading-relaxed">
              At Scandic Fusion, we combine Nordic innovation with cutting-edge technology
              to deliver solutions that don't just meet today's needs but anticipate tomorrow's challenges.
            </p>
            <p className="text-lg text-slate-300 mb-8 leading-relaxed">
              Our team of experts brings together decades of experience in software development,
              cloud architecture, and AI integration to create products that drive real business value.
            </p>
            <div className="space-y-4">
              {achievements.map((achievement, index) => (
                <div key={index} className="flex items-start gap-3">
                  <CheckCircle2 className="w-6 h-6 text-sky-400 flex-shrink-0 mt-0.5" />
                  <span className="text-slate-200">{achievement}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="relative">
            <div className="aspect-square bg-gradient-to-br from-sky-500/20 to-blue-600/20 rounded-3xl backdrop-blur-sm border border-sky-500/20 p-12 shadow-2xl">
              <div className="w-full h-full bg-gradient-to-br from-sky-500/10 to-blue-600/10 rounded-2xl border border-sky-500/10 flex items-center justify-center">
                <div className="text-center">
                  <div className="text-7xl font-bold text-sky-400 mb-4">10+</div>
                  <div className="text-xl text-slate-300">Years of Excellence</div>
                </div>
              </div>
            </div>
            <div className="absolute -bottom-6 -right-6 w-48 h-48 bg-sky-500/20 rounded-full blur-3xl"></div>
            <div className="absolute -top-6 -left-6 w-48 h-48 bg-blue-600/20 rounded-full blur-3xl"></div>
          </div>
        </div>
      </div>
    </section>
  );
}
