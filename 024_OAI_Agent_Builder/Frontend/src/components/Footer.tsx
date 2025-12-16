import { Github, Linkedin, Twitter, Mail } from 'lucide-react';

export function Footer() {
  return (
    <footer className="bg-slate-900 text-slate-300">
      <div className="max-w-7xl mx-auto px-6 py-16">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-12 mb-12">
          <div className="md:col-span-2">
            <h3 className="text-2xl font-bold text-white mb-4">Scandic Fusion</h3>
            <p className="text-slate-400 mb-6 leading-relaxed max-w-md">
              Leading the way in innovative technology solutions. We turn complex challenges
              into elegant, scalable systems that drive business growth.
            </p>
            <div className="flex gap-4">
              <a
                href="#"
                className="w-10 h-10 bg-slate-800 hover:bg-sky-500 rounded-lg flex items-center justify-center transition-colors duration-300"
              >
                <Twitter className="w-5 h-5" />
              </a>
              <a
                href="#"
                className="w-10 h-10 bg-slate-800 hover:bg-sky-500 rounded-lg flex items-center justify-center transition-colors duration-300"
              >
                <Linkedin className="w-5 h-5" />
              </a>
              <a
                href="#"
                className="w-10 h-10 bg-slate-800 hover:bg-sky-500 rounded-lg flex items-center justify-center transition-colors duration-300"
              >
                <Github className="w-5 h-5" />
              </a>
              <a
                href="#"
                className="w-10 h-10 bg-slate-800 hover:bg-sky-500 rounded-lg flex items-center justify-center transition-colors duration-300"
              >
                <Mail className="w-5 h-5" />
              </a>
            </div>
          </div>

          <div>
            <h4 className="text-white font-semibold mb-4">Company</h4>
            <ul className="space-y-3">
              <li>
                <a href="#" className="hover:text-sky-400 transition-colors">
                  About Us
                </a>
              </li>
              <li>
                <a href="#" className="hover:text-sky-400 transition-colors">
                  Careers
                </a>
              </li>
              <li>
                <a href="#" className="hover:text-sky-400 transition-colors">
                  Blog
                </a>
              </li>
              <li>
                <a href="#" className="hover:text-sky-400 transition-colors">
                  Press
                </a>
              </li>
            </ul>
          </div>

          <div>
            <h4 className="text-white font-semibold mb-4">Resources</h4>
            <ul className="space-y-3">
              <li>
                <a href="#" className="hover:text-sky-400 transition-colors">
                  Documentation
                </a>
              </li>
              <li>
                <a href="#" className="hover:text-sky-400 transition-colors">
                  API Reference
                </a>
              </li>
              <li>
                <a href="#" className="hover:text-sky-400 transition-colors">
                  Support
                </a>
              </li>
              <li>
                <a href="#" className="hover:text-sky-400 transition-colors">
                  Contact
                </a>
              </li>
            </ul>
          </div>
        </div>

        <div className="pt-8 border-t border-slate-800 text-center text-slate-500">
          <p>&copy; {new Date().getFullYear()} Scandic Fusion. All rights reserved.</p>
        </div>
      </div>
    </footer>
  );
}
