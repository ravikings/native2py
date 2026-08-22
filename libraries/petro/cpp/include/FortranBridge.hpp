//======================================================================
// FortranBridge.hpp - declarations for the Fortran 77 entry points
//
// Created  14-Sep-1992  T. Reinholt
// Revised  21-Jul-1994  added the f2c single underscore variant
// Revised  08-Mar-1998  Compaq Visual Fortran uppercase/stdcall names
//
// Name mangling is compiler dependent and there is no configure step
// in this build. The macro below has to be right for the target or
// you get link errors that name symbols nobody wrote.
//
//   HP-UX f77, SunPro, g77, gfortran   ->  lowercase + one underscore
//   AIX xlf                            ->  lowercase, no underscore
//   Compaq Visual Fortran on NT        ->  UPPERCASE, __stdcall
//
// EVERY argument is by reference. Fortran has no pass by value and
// forgetting an & here compiles cleanly and then reads the pointer
// value as a double. This has cost us two field reruns.
//======================================================================
#ifndef PETRO_FORTRAN_BRIDGE_HPP
#define PETRO_FORTRAN_BRIDGE_HPP

#if defined(_WIN32) && defined(CVF_FORTRAN)
#  define F77_NAME(lower, UPPER) UPPER
#  define F77_CALL __stdcall
#elif defined(_AIX)
#  define F77_NAME(lower, UPPER) lower
#  define F77_CALL
#else
#  define F77_NAME(lower, UPPER) lower##_
#  define F77_CALL
#endif

extern "C" {

// ---- pvtcor.f ------------------------------------------------------
void   F77_CALL F77_NAME(pvtini, PVTINI)(double* api, double* sgg,
                                         double* tres, int* icorr);
double F77_CALL F77_NAME(pvtrs,  PVTRS )(double* p);
double F77_CALL F77_NAME(pvtbo,  PVTBO )(double* p);
double F77_CALL F77_NAME(pvtvis, PVTVIS)(double* p);
double F77_CALL F77_NAME(pvtz,   PVTZ  )(double* p);
double F77_CALL F77_NAME(pvtbub, PVTBUB)(double* rs);
void   F77_CALL F77_NAME(pvtset, PVTSET)(double* p);
// NOTE: PVTERR takes a CHARACTER*(*) second argument. On every
// compiler we target the hidden length is appended as a trailing
// int passed BY VALUE, after all the visible arguments.
void   F77_CALL F77_NAME(pvterr, PVTERR)(int* icode, char* msg,
                                         int msg_len);

// ---- relperm.f -----------------------------------------------------
void   F77_CALL F77_NAME(krini,  KRINI )(double* swc, double* sor,
                                         double* sgc, double* ew,
                                         double* eo,  double* eg,
                                         double* krwm, double* krom,
                                         double* krgm);
void   F77_CALL F77_NAME(krload, KRLOAD)(double* sw, double* krw,
                                         double* kro, double* pcw,
                                         int* n);
double F77_CALL F77_NAME(krwat,  KRWAT )(double* sw);
double F77_CALL F77_NAME(krow,   KROW  )(double* sw);
double F77_CALL F77_NAME(krgas,  KRGAS )(double* sg);
double F77_CALL F77_NAME(kroil,  KROIL )(double* sw, double* sg);
double F77_CALL F77_NAME(pcow,   PCOW  )(double* sw);

// ---- hydrau.f ------------------------------------------------------
void   F77_CALL F77_NAME(bbdpdz, BBDPDZ)(double* p, double* qo,
                                         double* qw, double* qg,
                                         double* dia, double* theta,
                                         double* eps, double* dpdz,
                                         double* hl, int* ireg);
void   F77_CALL F77_NAME(traver, TRAVER)(double* pwh, double* qo,
                                         double* qw, double* qg,
                                         double* dia, double* eps,
                                         double* tvd, double* md,
                                         int* nseg, double* pbh);

// ---- flash.f -------------------------------------------------------
void   F77_CALL F77_NAME(cmpset, CMPSET)(int* n, double* z, double* pc,
                                         double* tc, double* w,
                                         double* mw);
void   F77_CALL F77_NAME(flash2, FLASH2)(double* p, double* t,
                                         double* beta, double* x,
                                         double* y, int* itout,
                                         int* istat);

// ---- simcor.f ------------------------------------------------------
void   F77_CALL F77_NAME(simini, SIMINI)(int* nx, int* ny, int* nz);
void   F77_CALL F77_NAME(grdset, GRDSET)(int* iprop, double* vals,
                                         int* n);
void   F77_CALL F77_NAME(trncal, TRNCAL)(void);
void   F77_CALL F77_NAME(equili, EQUILI)(double* woc, double* goc,
                                         double* pdat, double* ddat);
void   F77_CALL F77_NAME(step,   STEP  )(double* dtin, double* dtout,
                                         int* iconv);
double F77_CALL F77_NAME(fipoil, FIPOIL)(void);

// ---- wellib.f ------------------------------------------------------
void   F77_CALL F77_NAME(weladd, WELADD)(char* name, int* ityp,
                                         double* rw, double* skin,
                                         int* iw, int* jw, int* k1,
                                         int* k2, int* iwell,
                                         int name_len);
double F77_CALL F77_NAME(iprvog, IPRVOG)(double* pravg, double* pwf,
                                         double* qmax);
void   F77_CALL F77_NAME(nodal,  NODAL )(double* pravg, double* qmax,
                                         double* pwh, double* dia,
                                         double* eps, double* tvd,
                                         double* md, double* wcut,
                                         double* gor, double* qsol,
                                         double* pwfsol, int* iconv);

// ---- COMMON blocks reached directly. See PETRO.INC. The layout here
// ---- MUST match the INCLUDE deck exactly, field for field.
extern struct {
    double api, sgg, sgw, tres, pb, psep, tsep;
    int    ncomp;
} F77_NAME(fluid, FLUID);

extern struct {
    double bo, bg, bw, rsol, viso, visg, visw;
    double rhoo, rhog, rhow, zfac, co, cg, cw;
} F77_NAME(pvtout, PVTOUT);

extern struct {
    int ierr, iwarn, nitpvt, lunprt, lundbg, idbglv;
} F77_NAME(diag, DIAG);

} // extern "C"

#endif // PETRO_FORTRAN_BRIDGE_HPP
