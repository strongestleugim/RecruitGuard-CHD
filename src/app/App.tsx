import { Search, FileText, AlertCircle } from 'lucide-react';

export default function App() {
  return (
    <div className="min-h-screen bg-[#f5f6f7]">
      {/* Header */}
      <header className="bg-white border-b-2 border-gray-400">
        <div className="max-w-[1200px] mx-auto px-6 py-3.5">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[11px] uppercase tracking-wide text-gray-600 mb-0.5 font-medium">Department of Health – CHD CALABARZON</div>
              <h1 className="text-[20px] text-gray-900 font-medium">RecruitGuard-CHD Applicant Portal</h1>
            </div>
            <div className="flex items-center gap-5">
              <button className="bg-[#005a87] text-white px-5 py-2 hover:bg-[#004d72] transition-colors text-[13px] font-medium">
                Start New Application
              </button>
              <button className="bg-white text-gray-800 px-5 py-2 border border-gray-400 hover:bg-gray-50 transition-colors text-[13px] font-medium">
                Check Status
              </button>
              <span className="text-gray-400 text-[13px]">|</span>
              <a href="#" className="text-[12px] text-gray-500 hover:text-gray-800">Staff Portal</a>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-[1200px] mx-auto px-6 py-6">
        {/* Official Introduction */}
        <section className="bg-white border border-gray-300 px-6 py-4 mb-5">
          <h2 className="text-[15px] text-gray-900 mb-2 font-medium">Official Recruitment Application Portal</h2>
          <p className="text-[13px] leading-[1.6] text-gray-700">
            This portal provides access to current recruitment opportunities at DOH–CHD CALABARZON. Applicants may submit applications for <strong>Plantilla</strong> (permanent) or <strong>Contract of Service (COS)</strong> positions. All applications require OTP verification via registered mobile number or email. Applicants may track submission status using their reference number.
          </p>
        </section>

        {/* Guidance Section */}
        <section className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-5">
          <div className="bg-white border border-gray-300 px-4 py-3">
            <div className="flex items-start gap-2.5">
              <FileText className="w-4 h-4 text-gray-600 mt-0.5 flex-shrink-0" />
              <div>
                <h3 className="text-[13px] text-gray-900 mb-1 font-medium">Portal Purpose</h3>
                <p className="text-[12px] leading-[1.5] text-gray-600">
                  Submit applications for advertised positions. Review requirements and eligibility before proceeding.
                </p>
              </div>
            </div>
          </div>

          <div className="bg-white border border-gray-300 px-4 py-3">
            <div className="flex items-start gap-2.5">
              <AlertCircle className="w-4 h-4 text-gray-600 mt-0.5 flex-shrink-0" />
              <div>
                <h3 className="text-[13px] text-gray-900 mb-1 font-medium">Before Submission</h3>
                <p className="text-[12px] leading-[1.5] text-gray-600">
                  Prepare all required documents in PDF format. Ensure contact details are accurate for OTP verification.
                </p>
              </div>
            </div>
          </div>

          <div className="bg-white border border-gray-300 px-4 py-3">
            <div className="flex items-start gap-2.5">
              <Search className="w-4 h-4 text-gray-600 mt-0.5 flex-shrink-0" />
              <div>
                <h3 className="text-[13px] text-gray-900 mb-1 font-medium">After Submission</h3>
                <p className="text-[12px] leading-[1.5] text-gray-600">
                  Save your reference number. Use it to check application status and receive official correspondence.
                </p>
              </div>
            </div>
          </div>
        </section>

        {/* Main Content: Available Openings */}
        <section>
          <div className="mb-4 pb-3 border-b-2 border-gray-300">
            <h2 className="text-[17px] text-gray-900 mb-1.5 font-medium">Available Openings and Application Paths</h2>
            <p className="text-[13px] text-gray-600 leading-[1.5]">
              Applicants must select the correct recruitment path. Plantilla and Contract of Service (COS) positions are handled separately. Review each opening carefully before applying.
            </p>
          </div>

          {/* Plantilla Recruitment */}
          <div className="bg-white border-2 border-gray-400 mb-4">
            <div className="border-b-2 border-gray-400 bg-[#f8f9fa] px-5 py-2.5">
              <h3 className="text-[14px] text-gray-900 font-medium">Plantilla Recruitment (Permanent Positions)</h3>
            </div>
            <div className="px-5 py-4">
              <div className="border border-gray-300 bg-white">
                <div className="border-l-[5px] border-[#005a87] px-5 py-4">
                  <div className="flex items-start justify-between gap-6 mb-4">
                    <div>
                      <h4 className="text-[15px] text-gray-900 font-medium mb-1">Administrative Aide VI</h4>
                      <p className="text-[12px] text-gray-600">Position Code: HRMS-AA6-2026-001</p>
                    </div>
                    <button className="bg-[#005a87] text-white px-6 py-2 hover:bg-[#004d72] transition-colors text-[13px] font-medium whitespace-nowrap">
                      Apply Now
                    </button>
                  </div>

                  <div className="border-t border-gray-200 pt-3">
                    <div className="grid grid-cols-2 gap-x-10 gap-y-2.5">
                      <div className="flex">
                        <span className="text-[11px] text-gray-600 uppercase tracking-wider font-medium w-[110px] flex-shrink-0">Office/Unit:</span>
                        <span className="text-[13px] text-gray-900">Human Resource Management Section</span>
                      </div>
                      <div className="flex">
                        <span className="text-[11px] text-gray-600 uppercase tracking-wider font-medium w-[110px] flex-shrink-0">Level:</span>
                        <span className="text-[13px] text-gray-900">SG-6 (₱13,000 – ₱16,860/month)</span>
                      </div>
                      <div className="flex">
                        <span className="text-[11px] text-gray-600 uppercase tracking-wider font-medium w-[110px] flex-shrink-0">Intake Type:</span>
                        <span className="text-[13px] text-gray-900">Competitive Examination</span>
                      </div>
                      <div className="flex">
                        <span className="text-[11px] text-gray-600 uppercase tracking-wider font-medium w-[110px] flex-shrink-0">Closing Date:</span>
                        <span className="text-[13px] text-gray-900 font-medium">April 15, 2026 (5:00 PM)</span>
                      </div>
                    </div>
                  </div>

                  <div className="border-t border-gray-200 mt-3 pt-3">
                    <div className="flex">
                      <span className="text-[11px] text-gray-600 uppercase tracking-wider font-medium w-[150px] flex-shrink-0">Key Qualifications:</span>
                      <span className="text-[13px] text-gray-700 leading-[1.6]">
                        Bachelor's degree in any field; CSC Sub-professional eligible; minimum 1 year relevant administrative experience; proficient in MS Office applications (Word, Excel, PowerPoint)
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <div className="mt-3 text-[12px] text-gray-600 flex items-center justify-between">
                <span>Showing 1 open position</span>
                <span>Last updated: March 27, 2026 at 9:30 AM</span>
              </div>
            </div>
          </div>

          {/* COS Recruitment */}
          <div className="bg-white border-2 border-gray-400">
            <div className="border-b-2 border-gray-400 bg-[#f8f9fa] px-5 py-2.5">
              <h3 className="text-[14px] text-gray-900 font-medium">Contract of Service (COS) Recruitment</h3>
            </div>
            <div className="px-5 py-6">
              <div className="text-center py-4">
                <div className="inline-flex items-center justify-center w-10 h-10 bg-gray-100 border border-gray-300 mb-2">
                  <FileText className="w-5 h-5 text-gray-500" />
                </div>
                <p className="text-[13px] text-gray-800 mb-1 font-medium">No Open COS Positions</p>
                <p className="text-[12px] text-gray-600 leading-[1.5]">
                  There are currently no active Contract of Service recruitment entries. Please check back for future postings.
                </p>
              </div>
            </div>
          </div>
        </section>

        {/* Footer Notice */}
        <footer className="mt-6 pt-4 border-t border-gray-300">
          <div className="text-center">
            <p className="text-[11px] text-gray-700 font-medium mb-0.5">
              Department of Health – Center for Health Development CALABARZON (Region IV-A)
            </p>
            <p className="text-[11px] text-gray-600">
              Official Recruitment Application Interface • RecruitGuard-CHD System
            </p>
          </div>
        </footer>
      </main>
    </div>
  );
}
