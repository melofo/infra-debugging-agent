import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  // standalone：构建出一个只含运行所需文件的最小目录，Docker 镜像因此小得多（见 frontend/Dockerfile）。
  output: "standalone",
  cacheComponents: true,
  partialPrefetching: true,
};

export default nextConfig;
