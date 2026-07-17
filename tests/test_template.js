const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync("src/theme/index.html", "utf8");
const scripts = html
  .split("<script>")
  .slice(1)
  .map((part) => part.split("</script>")[0]);

scripts.forEach((script, index) => {
  try {
    new Function(script);
  } catch (error) {
    throw new Error(`inline script ${index} has invalid syntax: ${error.message}`);
  }
});

const context = {};
vm.runInNewContext(scripts[1], context);
context.qrcode.stringToBytes = context.qrcode.stringToBytesFuncs["UTF-8"];
const code = context.qrcode(0, "M");
code.addData("vless://local-qr-test", "Byte");
code.make();
const dataUrl = code.createDataURL(4, 16);
if (!dataUrl.startsWith("data:image/gif;base64,")) {
  throw new Error("local QR generator did not return a GIF data URL");
}

console.log(`validated ${scripts.length} inline scripts and local QR generation`);
