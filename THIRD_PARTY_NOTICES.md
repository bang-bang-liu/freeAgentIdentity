# Third-party notices

ChatGPT login landing-page handling, workspace selection and the Cloudflare
challenge runtime follow the flow in
[poxiao33/toSub2](https://github.com/poxiao33/toSub2), commit
`8548397e89bf80e508eda64a87e0d556d43abc84` (`src/protocol-login.mjs`,
`src/tls-transport.mjs`, and `src/cloudflare-ctf/`). The runtime lives in
`registration_core/cloudflare/`; the Python bridge adds Windows pipe handling,
task cancellation and shared login deadlines.

MIT License

Copyright (c) 2026 poxiao33

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
