# 端到端测试用的"假电脑 B"：Ubuntu 20.04 + SSH + sudo + dhclient，
# 用桩程序代替 systemd/loginctl（容器里没有 systemd）。
FROM ubuntu:20.04
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       openssh-server sudo python3 iproute2 isc-dhcp-client iputils-ping procps \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash robot && echo 'robot:robot123' | chpasswd && adduser robot sudo \
    && mkdir -p /run/sshd /etc/gdm3 /etc/X11 /etc/systemd/system \
    && echo /usr/sbin/gdm3 > /etc/X11/default-display-manager \
    && touch /etc/systemd/system/display-manager.service
COPY gdm-custom.conf /etc/gdm3/custom.conf
COPY systemctl loginctl fake-nm-dhcp /usr/local/bin/
COPY fake-nm.conf /etc/dhcp/fake-nm.conf
RUN chmod 755 /usr/local/bin/systemctl /usr/local/bin/loginctl /usr/local/bin/fake-nm-dhcp
CMD ["/usr/sbin/sshd", "-D", "-e"]
