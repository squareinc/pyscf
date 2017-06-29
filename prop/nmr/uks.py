#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#


import numpy
from pyscf import lib
from pyscf.lib import logger
from pyscf.scf import _vhf
from pyscf.scf.hf import _attach_mo
from pyscf.dft import numint
from pyscf.prop.nmr import rks as rks_nmr
from pyscf.prop.nmr import uhf as uhf_nmr


def get_vxc_giao(ni, mol, grids, xc_code, dms, max_memory=2000, verbose=None):
    xctype = ni._xc_type(xc_code)
    make_rhoa, nset, nao = ni._gen_rho_evaluator(mol, dms[0], hermi=1)
    make_rhob            = ni._gen_rho_evaluator(mol, dms[1], hermi=1)[0]
    ngrids = len(grids.weights)
    BLKSIZE = numint.BLKSIZE
    blksize = min(int(max_memory/12*1e6/8/nao/BLKSIZE)*BLKSIZE, ngrids)
    shls_slice = (0, mol.nbas)
    ao_loc = mol.ao_loc_nr()

    vmat = numpy.zeros((2,3,nao,nao))
    if xctype == 'LDA':
        buf = numpy.empty((4,blksize,nao))
        ao_deriv = 0
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory,
                                 blksize=blksize, buf=buf):
            rho_a = make_rhoa(0, ao, mask, 'LDA')
            rho_b = make_rhob(0, ao, mask, 'LDA')
            vxc = ni.eval_xc(xc_code, (rho_a, rho_b), 1, deriv=1)[1]
            vrho = vxc[0]
            giao = mol.eval_gto('GTOval_ig', coords, comp=3,
                                non0tab=mask, out=buf[1:])
            aow = numpy.einsum('pi,p->pi', ao, weight*vrho[:,0])
            vmat[0,0] += numint._dot_ao_ao(mol, aow, giao[0], mask, shls_slice, ao_loc)
            vmat[0,1] += numint._dot_ao_ao(mol, aow, giao[1], mask, shls_slice, ao_loc)
            vmat[0,2] += numint._dot_ao_ao(mol, aow, giao[2], mask, shls_slice, ao_loc)
            aow = numpy.einsum('pi,p->pi', ao, weight*vrho[:,1])
            vmat[1,0] += numint._dot_ao_ao(mol, aow, giao[0], mask, shls_slice, ao_loc)
            vmat[1,1] += numint._dot_ao_ao(mol, aow, giao[1], mask, shls_slice, ao_loc)
            vmat[1,2] += numint._dot_ao_ao(mol, aow, giao[2], mask, shls_slice, ao_loc)
            rho = vxc = vrho = aow = None
    elif xctype == 'GGA':
        buf = numpy.empty((10,blksize,nao))
        ao_deriv = 1
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory,
                                 blksize=blksize, buf=buf):
            rho_a = make_rhoa(0, ao, mask, 'GGA')
            rho_b = make_rhob(0, ao, mask, 'GGA')
            vxc = ni.eval_xc(xc_code, (rho_a,rho_b), 1, deriv=1)[1]
            vrho, vsigma = vxc[:2]
            giao = mol.eval_gto('GTOval_ig', coords, 3, non0tab=mask, out=buf[4:])

            wva = numpy.empty_like(rho_a)
            wva[0]  = weight * vrho[:,0]
            wva[1:] = rho_a[1:] * (weight * vsigma[:,0] * 2)  # sigma_uu
            wva[1:]+= rho_b[1:] * (weight * vsigma[:,1])      # sigma_ud
            wvb = numpy.empty_like(rho_b)
            wvb[0]  = weight * vrho[:,1]
            wvb[1:] = rho_b[1:] * (weight * vsigma[:,2] * 2)  # sigma_dd
            wvb[1:]+= rho_a[1:] * (weight * vsigma[:,1])      # sigma_ud

            aow = numpy.einsum('npi,np->pi', ao[:4], wva)
            vmat[0,0] += numint._dot_ao_ao(mol, aow, giao[0], mask, shls_slice, ao_loc)
            vmat[0,1] += numint._dot_ao_ao(mol, aow, giao[1], mask, shls_slice, ao_loc)
            vmat[0,2] += numint._dot_ao_ao(mol, aow, giao[2], mask, shls_slice, ao_loc)
            aow = numpy.einsum('npi,np->pi', ao[:4], wvb)
            vmat[1,0] += numint._dot_ao_ao(mol, aow, giao[0], mask, shls_slice, ao_loc)
            vmat[1,1] += numint._dot_ao_ao(mol, aow, giao[1], mask, shls_slice, ao_loc)
            vmat[1,2] += numint._dot_ao_ao(mol, aow, giao[2], mask, shls_slice, ao_loc)

            giao = mol.eval_gto('GTOval_ipig', coords, 9, non0tab=mask, out=buf[1:])
            rks_nmr._gga_sum_(vmat[0], mol, ao, giao, wva, mask, shls_slice, ao_loc)
            rks_nmr._gga_sum_(vmat[1], mol, ao, giao, wvb, mask, shls_slice, ao_loc)
            rho = vxc = vrho = vsigma = wv = aow = None
    else:
        raise NotImplementedError('meta-GGA')

    return vmat - vmat.transpose(0,1,3,2)


class NMR(uhf_nmr.NMR):
    def make_h10(self, mol=None, dm0=None, gauge_orig=None):
        if mol is None: mol = self.mol
        if dm0 is None: dm0 = self._scf.make_rdm1()
        if gauge_orig is None: gauge_orig = self.gauge_orig

        if gauge_orig is None:
            log = logger.Logger(self.stdout, self.verbose)
            log.debug('First-order GIAO Fock matrix')

            mf = self._scf
            ni = mf._numint
            hyb = ni.hybrid_coeff(mf.xc, spin=mol.spin)

            mem_now = lib.current_memory()[0]
            max_memory = max(2000, mf.max_memory*.9-mem_now)
            dm0 = _attach_mo(dm0, mf.mo_coeff, mf.mo_occ)  # to improve get_vxc_giao efficiency
            h1 = get_vxc_giao(ni, mol, mf.grids, mf.xc, dm0,
                              max_memory=max_memory, verbose=mf.verbose)

            intor = mol._add_suffix('int2e_ig1')
            if abs(hyb) > 1e-10:
                vj, vk = _vhf.direct_mapdm(intor,  # (g i,j|k,l)
                                           'a4ij', ('lk->s1ij', 'jk->s1il'),
                                           dm0, 3, # xyz, 3 components
                                           mol._atm, mol._bas, mol._env)
                vk = vk - vk.transpose(0,1,3,2)
                h1 += vj[0] + vj[1] - hyb * vk
            else:
                vj = _vhf.direct_mapdm(intor, 'a4ij', 'lk->s1ij',
                                       dm0, 3, mol._atm, mol._bas, mol._env)
                h1 += vj[0] + vj[1]

            h1 += .5 * mol.intor('int1e_giao_irjxp', 3)
            h1 += mol.intor_asymmetric('int1e_ignuc', 3)
            h1 += mol.intor('int1e_igkin', 3)
        else:
            mol.set_common_origin(gauge_orig)
            h1 = .5 * mol.intor('int1e_cg_irxp', 3)
            h1 = (h1, h1)
        lib.chkfile.dump(self.chkfile, 'nmr/h1', h1)
        return h1

    def solve_mo1(self, mo_energy=None, mo_occ=None, h1=None, s1=None,
                  with_cphf=None):
        if with_cphf is None:
            with_cphf = self.cphf
        libxc = self._scf._numint.libxc
        with_cphf = with_cphf and libxc.is_hybrid_xc(self._scf.xc)
        return uhf_nmr.NMR.solve_mo1(self, mo_energy, mo_occ, h1, s1, with_cphf)


if __name__ == '__main__':
    from pyscf import gto
    from pyscf import dft
    mol = gto.Mole()
    mol.verbose = 0
    mol.output = None

    mol.atom = [
        ['Ne' , (0. , 0. , 0.)], ]
    mol.basis='631g'
    mol.build()

    mf = dft.UKS(mol)
    mf.kernel()
    nmr = NMR(mf)
    msc = nmr.kernel() # _xx,_yy,_zz = 55.131555
    print(msc)

    mol.atom = [
        [1   , (0. , 0. , .917)],
        ['F' , (0. , 0. , 0.  )], ]
    mol.basis = {'H': '6-31g',
                 'F': '6-31g',}
    mol.build()

    mf = dft.UKS(mol)
    mf.kernel()
    nmr = NMR(mf)
    msc = nmr.kernel() # _xx,_yy = 368.881201, _zz = 482.413385
    print(msc)

    mol.basis = 'ccpvdz'
    mol.build(0, 0)
    mf = dft.UKS(mol)
    mf.xc = 'b3lyp'
    mf.kernel()
    nmr = NMR(mf)
    msc = nmr.kernel() # _xx,_yy = 387.102778, _zz = 482.207925
    print(msc)

