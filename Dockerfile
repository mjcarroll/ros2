FROM ubuntu:20.04

RUN apt update && apt upgrade -y && apt install -y wget && apt clean

RUN wget -O Mambaforge.sh  "https://github.com/conda-forge/miniforge/releases/latest/download/Mambaforge-Linux-x86_64.sh" && \
    bash Mambaforge.sh -b -p "${HOME}/conda" && \
    rm Mambaforge.sh && \
    echo '. ${HOME}/conda/etc/profile.d/conda.sh' >> ${HOME}/.profile && \
    echo '. ${HOME}/conda/etc/profile.d/mamba.sh' >> ${HOME}/.profile

RUN wget -O buildcache-linux.tar.gz https://github.com/mbitsnbites/buildcache/releases/download/v0.28.1/buildcache-linux.tar.gz && \
    tar xvzf buildcache-linux.tar.gz -C ${HOME} && \
    rm buildcache-linux.tar.gz

ADD .github/ci/environment.base.yaml environment.base.yaml
RUN . ${HOME}/.profile && \
    mamba activate && \
    mamba env update -f environment.base.yaml

ADD .github/ci/environment.yaml environment.yaml
RUN . ${HOME}/.profile && \
    mamba activate && \
    mamba env update -f environment.yaml

ADD .github/ci/environment.linux-64.yaml environment.linux-64.yaml
RUN . ${HOME}/.profile && \
    mamba activate && \
    mamba env update -f environment.linux-64.yaml

ADD ros2.repos ros2.repos
RUN . ${HOME}/.profile && mamba activate ros-ws && \
    mkdir -p /ros2_ws/src && vcs import --input ros2.repos /ros2_ws/src

ADD .github/ci/colcon_defaults.yaml /root/colcon_defaults.yaml
ADD setup.sh /root/setup.sh
