import { Brain, Shield, Zap, Globe, TrendingUp, Lock } from 'lucide-react';

const features = [
  {
    icon: Brain,
    title: 'AI-Powered Solutions',
    description: 'Leverage advanced artificial intelligence to automate processes and gain insights from your data.',
  },
  {
    icon: Shield,
    title: 'Enterprise Security',
    description: 'Bank-grade security measures to protect your data and ensure compliance with global standards.',
  },
  {
    icon: Zap,
    title: 'Lightning Fast',
    description: 'Optimized performance that scales with your business needs without compromising speed.',
  },
  {
    icon: Globe,
    title: 'Global Reach',
    description: 'Deploy solutions across multiple regions with seamless integration and localization support.',
  },
  {
    icon: TrendingUp,
    title: 'Scalable Infrastructure',
    description: 'Built to grow with your business, from startup to enterprise without missing a beat.',
  },
  {
    icon: Lock,
    title: 'Privacy First',
    description: 'Your data remains yours. We prioritize privacy and transparency in everything we do.',
  },
];

export function Features() {
  return (
    <section className="py-24 bg-white">
      <div className="max-w-7xl mx-auto px-6">
        <div className="text-center mb-16">
          <h2 className="text-4xl md:text-5xl font-bold text-slate-900 mb-4">
            Why Choose Scandic Fusion
          </h2>
          <p className="text-xl text-slate-600 max-w-2xl mx-auto">
            Powerful features designed to accelerate your digital transformation journey
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8">
          {features.map((feature, index) => {
            const Icon = feature.icon;
            return (
              <div
                key={index}
                className="group p-8 bg-slate-50 hover:bg-gradient-to-br hover:from-sky-50 hover:to-blue-50 rounded-2xl transition-all duration-300 hover:shadow-xl hover:scale-105 border border-slate-100 hover:border-sky-200"
              >
                <div className="w-14 h-14 bg-sky-500 group-hover:bg-sky-600 rounded-xl flex items-center justify-center mb-6 transition-colors duration-300 shadow-lg group-hover:shadow-sky-500/25">
                  <Icon className="w-7 h-7 text-white" />
                </div>
                <h3 className="text-xl font-bold text-slate-900 mb-3">
                  {feature.title}
                </h3>
                <p className="text-slate-600 leading-relaxed">
                  {feature.description}
                </p>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
