# \\/ Westermo - WeOS container image generator
#
# Copyright (C) 2025  Westermo Network Technologies AB
#
# Author: Patrik Olsson <patrik.olsson@westermo.com>
#
# Description:
# Utility script for generating and signing container images for Westermo container enabled platforms from Dockerfiles,
# OCI bundles or root filesystems.
#

# TODO: Fix broken OCI generator
# TODO: Fix broken kernel config, NF_TABLES_INET is missing

import os
import json
import shutil
import tarfile
import uuid
import subprocess
import argparse

def extract_rootfs_from_oci_bundle(bundle_dir, output_dir):
        absolute_bundle_path = os.path.abspath(bundle_dir)

        index_json_path = os.path.join(absolute_bundle_path, 'index.json')
        if not os.path.exists(index_json_path):
                print(f"Error: {index_json_path} does not exist")
                return

        with open(index_json_path, 'r') as f:
                index_data = json.load(f)

        manifest_digest = index_data['manifests'][0]['digest']
        manifest_path = os.path.join(absolute_bundle_path, 'blobs', 'sha256', manifest_digest.split(':')[1])

        if not os.path.exists(manifest_path):
                print(f"Error: {manifest_path} does not exist")
                return

        with open(manifest_path, 'r') as f:
                manifest_data = json.load(f)

        layers = manifest_data['layers']
        layer_digests = [layer['digest'] for layer in layers]

        for diff_id in layer_digests:
                layer_path = os.path.join(absolute_bundle_path, 'blobs', 'sha256', diff_id.split(':')[1])
                if not os.path.exists(layer_path):
                        print(f"Error: {layer_path} does not exist")
                        return

                with tarfile.open(layer_path, 'r') as tar:
                        tar.extractall(path=output_dir)

def extract_container_metadata(bundle_dir):
        index_json_path = os.path.join(bundle_dir, 'index.json')
        if not os.path.exists(index_json_path):
                print(f"Error: {index_json_path} does not exist")
                return

        with open(index_json_path, 'r') as f:
                index_data = json.load(f)

        manifest_digest = index_data['manifests'][0]['digest']
        manifest_path = os.path.join(bundle_dir, 'blobs', 'sha256', manifest_digest.split(':')[1])

        if not os.path.exists(manifest_path):
                print(f"Error: {manifest_path} does not exist")
                return

        with open(manifest_path, 'r') as f:
                manifest_data = json.load(f)

        config_digest = manifest_data['config']['digest']
        config_path = os.path.join(bundle_dir, 'blobs', 'sha256', config_digest.split(':')[1])

        if not os.path.exists(config_path):
                print(f"Error: {config_path} does not exist")
                return

        with open(config_path, 'r') as f:
                config_data = json.load(f)

        entrypoint = config_data['config'].get('Entrypoint', [])
        cmd = config_data['config'].get('Cmd', [])
        working_dir = config_data['config'].get('WorkingDir', '')
        ports = config_data['config'].get('ExposedPorts', {}).keys()
        env_vars = config_data['config'].get('Env', [])

        metadata = {
                'entrypoint': entrypoint,
                'cmd': cmd,
                'working_dir': working_dir,
                'ports': list(ports),
                'env_vars': env_vars
        }

        print("Container Metadata:")
        print(json.dumps(metadata, indent=4))

        return metadata

def extract_oci_bundle(tar_path, extract_to):
        if not tarfile.is_tarfile(tar_path):
                print(f"Error: {tar_path} is not a valid tar file")
                return

        with tarfile.open(tar_path, 'r') as tar:
                tar.extractall(path=extract_to)

def create_squashfs(source_dir, output_file):
        try:
                subprocess.run(['mksquashfs', source_dir, output_file, '-no-xattrs', '-comp', 'xz', '-all-root', '-b', '131072'], check=True)
                print(f"SquashFS created at {output_file}")
        except subprocess.CalledProcessError as e:
                print(f"Error creating SquashFS: {e}")
        except FileNotFoundError:
                print("Error: mksquashfs command not found. Please install squashfs-tools.")

def generate_its_file(description, version, output_file):
        its_template ='''/dts-v1/;
/ {
                description = "####DESCRIPTION####";
                version = "####VERSION####";
                #address-cells = <1>;
                images {
                                filesystem-1 {
                                                description = "Container rootfs";
                                                data = /incbin/("./rootfs.sqfs");
                                                type = "filesystem";
                                                compression = "none";
                                                signature-1 {
                                                                algo = "sha256,rsa2048";
                                                };
                                };

                };

                configurations {
                                /* Intentionally left empty */
                };
};
'''
        its_content = its_template.replace("####DESCRIPTION####", description).replace("####VERSION####", version)
        with open(output_file, 'w') as f:
                f.write(its_content)

# ERROR: OCI exporter is not supported for the docker driver.
# Switch to a different driver, or turn on the containerd image store, and try again.
# Learn more at https://docs.docker.com/go/build-exporters/
# 
# Solution: docker buildx create --use
def generate_oci_bundle_from_dockerfile(dockerfile_path, output_file, architecture):
        if not os.path.exists(dockerfile_path):
                print(f"Error: {dockerfile_path} does not exist")
                return
        
        print(f"Generating OCI bundle for {architecture} architecture")

        try:
                # Check if we have an existing ocibuilder, else create it.
                result = subprocess.check_output('docker builder ls', shell=True, text=True)
                if 'ocibuilder' not in result:
                    subprocess.run([
                        'docker', 'buildx', 'create', '--driver', 'docker-container', '--driver-opt',
                        'image=moby/buildkit:master,network=host', '--name=ocibuilder', '--bootstrap'], check=True)

                subprocess.run([
                        'docker', 'buildx', 'build', '--platform', architecture, '-f', dockerfile_path, '-o',
                        f'type=oci,dest={output_file}', '.', '--builder=ocibuilder' ], check=True)
                print(f"OCI bundle generated at {output_file}")
        except subprocess.CalledProcessError as e:
                print(f"Error generating OCI bundle: {e}")
        except FileNotFoundError:
                print("Error: Docker command not found. Please install Docker.")

def generate_itb_from_its(its_file, output_file, key_file):
        try:
                if not key_file:
                        print("Warning: No key provided, the ITB file will not be signed")
                        subprocess.run(['mkimage', '-f', its_file, output_file], check=True)
                else:
                        subprocess.run(['mkimage', '-f', its_file, '-G', key, output_file], check=True)
        except subprocess.CalledProcessError as e:
                print(f"Error creating or signing ITB: {e}")
        except FileNotFoundError:
                print("Error: mkimage command not found. Please install U-Boot tools.")

def process_oci_bundle(input, rootfs_path, tmp_dir, output_file, version, description, key):
        extract_rootfs_from_oci_bundle(input, rootfs_path)
        sqfs_path = os.path.join(tmp_dir, 'rootfs.sqfs')
        create_squashfs(rootfs_path, sqfs_path)
        assemble_wmopkg(tmp_dir, output_file, version, description, key)
        extract_container_metadata(input)

def assemble_wmopkg(tmp_dir, output_file, version, description, key):
        its_path = os.path.join(tmp_dir, 'package.its')
        generate_its_file(description, version, its_path)
        filename = output_file + '-' + version + '.wmopkg'
        generate_itb_from_its(its_path, filename, key)

def create_tmp_dir():
        tmp_dir = os.path.join(os.getcwd(), 'tmp_' + str(uuid.uuid4()))
        os.makedirs(tmp_dir)
        return tmp_dir

def remove_tmp_dir(tmp_dir):
        if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)

def is_docker_file(file_path):
        try:
                result = subprocess.run(['file', '-b', file_path], capture_output=True, text=True)
                return 'ASCII text' in result.stdout
        except subprocess.CalledProcessError as e:
                print(f"Error checking file type: {e}")
                return False
        except FileNotFoundError:
                print("Error: file command not found. Please install file package.")
                return False

def is_squashfs_file(file_path):
        try:
                result = subprocess.run(['file', '-b', file_path], capture_output=True, text=True)
                return 'Squashfs' in result.stdout
        except subprocess.CalledProcessError as e:
                print(f"Error checking file type: {e}")
                return False
        except FileNotFoundError:
                print("Error: file command not found. Please install file package.")
                return False

def is_tar_file(file_path):
        return tarfile.is_tarfile(file_path)

if __name__ == "__main__":
        parser = argparse.ArgumentParser(description='Generate container package')
        parser.add_argument('-i', '--input', required=True, help='Input file or directory')
        parser.add_argument('-t', '--type', required=True, help='Type of input file')
        parser.add_argument('-n', '--name', required=True, help='Output file name')
        parser.add_argument('-v', '--version', required=True, help='Version of the package')
        parser.add_argument('-d', '--description', required=True, help='Description of the package')
        parser.add_argument('-a', '--architecture', help='Target architecture')
        parser.add_argument('-k', '--key', help='Key file for signing')
        args = parser.parse_args()

        input = args.input
        type = args.type
        output_file = args.name
        version = args.version
        description = args.description
        architecture = args.architecture
        key = args.key

        bundle_path = None
        tmp_dir = create_tmp_dir()
        rootfs_path = os.path.join(tmp_dir, 'rootfs')

        if type == 'docker':
                if not architecture:
                        parser.print_usage()
                else:
                        oci_bundle_file = os.path.join(tmp_dir, 'bundle.tar')
                        generate_oci_bundle_from_dockerfile(input, oci_bundle_file, architecture)
                        bundle_path = os.path.join(tmp_dir, 'bundle')
                        extract_oci_bundle(oci_bundle_file, bundle_path)
                        process_oci_bundle(bundle_path, rootfs_path, tmp_dir, output_file, version, description, key)
        elif type == 'oci':
                bundle_path = os.path.join(tmp_dir, 'bundle')
                extract_oci_bundle(input, bundle_path)
                process_oci_bundle(bundle_path, rootfs_path, tmp_dir, output_file, version, description, key)
        elif type == 'rootfs':
                extract_oci_bundle(input, rootfs_path)
                sqfs_path = os.path.join(tmp_dir, 'rootfs.sqfs')
                create_squashfs(rootfs_path, sqfs_path)
                assemble_wmopkg(tmp_dir, output_file, version, description, key)
        else:
                print(f"Error: {type} is not a valid type, must be docker, oci or rootfs")

        remove_tmp_dir(tmp_dir)
